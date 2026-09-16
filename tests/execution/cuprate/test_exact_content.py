"""Regressions for the exact-content authoring kit."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aletheia.execution.cuprate.exact_content import (
    combined_outcome_bin_id,
    diagnostic_assessment_template,
    diagnostic_observation_bytes,
)
from aletheia.observations.scientific_bridge import BridgeValidationDisposition
from aletheia.research_controller.external_rpc import (
    CuprateDiagnosticResult,
    CuprateDopingStratificationOutcome,
    CuprateMatchedControlOutcome,
)
from aletheia.research_kernel.schemas import canonical_json_bytes, canonical_sha256

_TESTS = Path(__file__).resolve().parents[2]
for _fixture_dir in (_TESTS / "observations", _TESTS / "research_controller"):
    sys.path.insert(0, str(_fixture_dir))

from test_f9_v2_validation import _f9_case  # noqa: E402
from test_scientific_bridge import _raw_run  # noqa: E402

_D1_BINS = ("family_excess_ci_excludes_zero", "family_excess_ci_includes_zero")
_D2_BINS = ("error_concentrates_at_extremes", "no_stratification_structure")


def _result(d1_bin=_D1_BINS[0], d2_bin=_D2_BINS[0]) -> CuprateDiagnosticResult:
    return CuprateDiagnosticResult(
        dataset_content_sha256="a" * 64,
        doping_optimum=0.16,
        analyzed_rows=60,
        dropped_off_batch_rows=2,
        d1_matched_control=CuprateMatchedControlOutcome(
            mae_cuprate=8.0,
            mae_matched=2.0,
            mae_non_all=3.0,
            excess_over_matched=6.0,
            ci=(1.0, 4.0),
            survives=True,
            n_cuprate=10,
            outcome_bin=d1_bin,
        ),
        d2_doping_stratification=CuprateDopingStratificationOutcome(
            family_holdout_rows=24,
            deviation_threshold=0.2,
            effect=5.0,
            ctrl_p95=1.0,
            concentrates=True,
            outcome_bin=d2_bin,
        ),
    )


@pytest.mark.parametrize("d1_bin", _D1_BINS)
@pytest.mark.parametrize("d2_bin", _D2_BINS)
def test_combined_outcome_bin_id_is_mechanical_over_both_bins(d1_bin, d2_bin):
    assert combined_outcome_bin_id(_result(d1_bin, d2_bin)) == f"cuprate.d1:{d1_bin}.d2:{d2_bin}"


def test_unknown_bins_fail_closed():
    result = _result()
    tampered = result.model_copy(
        update={
            "d1_matched_control": result.d1_matched_control.model_copy(
                update={"outcome_bin": "bogus"}
            )
        }
    )
    with pytest.raises(ValueError, match="unknown outcome bin"):
        combined_outcome_bin_id(tampered)


def test_observation_bytes_are_the_canonical_result_bytes():
    result = _result()
    assert diagnostic_observation_bytes(result) == canonical_json_bytes(result)


def test_template_refuses_a_raw_run_that_lacks_the_result_bytes(monkeypatch: pytest.MonkeyPatch):
    raw_run = _raw_run(_f9_case(monkeypatch))
    with pytest.raises(ValueError, match="canonical diagnostic result bytes"):
        diagnostic_assessment_template(
            raw_run=raw_run,
            result=_result(),
            disposition=BridgeValidationDisposition.VALIDATED_CONFIRMATION,
        )


def test_template_pins_the_result_bytes_and_the_authored_disposition(
    monkeypatch: pytest.MonkeyPatch,
):
    result = _result()
    raw_run = _raw_run(
        _f9_case(monkeypatch),
        artifact_entry_updates={"content_sha256": canonical_sha256(result)},
    )
    template = diagnostic_assessment_template(
        raw_run=raw_run,
        result=result,
        disposition=BridgeValidationDisposition.VALIDATED_CONFIRMATION,
    )
    assert template.raw_observation_content_sha256 == canonical_sha256(result)
    assert template.outcome_bin_id == combined_outcome_bin_id(result)
    assert template.disposition is BridgeValidationDisposition.VALIDATED_CONFIRMATION
    assert template.blocker_codes == ()
    assert len(template.template_sha256) == 64 and len(template.lookup_sha256) == 64

    rejected = diagnostic_assessment_template(
        raw_run=raw_run,
        result=result,
        disposition=BridgeValidationDisposition.REJECTED_SCIENTIFIC,
        blocker_codes=("f9-v2:sample-blocker",),
    )
    assert rejected.blocker_codes == ("f9-v2:sample-blocker",)
    assert rejected.template_sha256 != template.template_sha256
