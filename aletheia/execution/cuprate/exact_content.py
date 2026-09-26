"""Exact-content assessment authoring kit for the cuprate diagnostic.

The F9-v2 catalog pins one authored disposition to the exact observation
bytes.  This kit keeps the authoring discipline closed: the typed result
fixes only the canonical combined outcome-bin id — a mechanical
concatenation of the two preregistered bins — while the disposition and
blocker codes are always authored inputs supplied per the preregistered
AnalysisPlan rule, never derived from the run's numbers.

Live templates need a quest harness's context ids (quest, action, slot,
graph snapshot), so authoring against a real raw run happens at the
commissioning dry run; this module and its fixture tests ship first.
"""

from __future__ import annotations

from aletheia.execution.cuprate.diagnostics import (
    ERROR_CONCENTRATES_AT_EXTREMES,
    FAMILY_EXCESS_CI_EXCLUDES_ZERO,
    FAMILY_EXCESS_CI_INCLUDES_ZERO,
    NO_STRATIFICATION_STRUCTURE,
)
from aletheia.observations.f9_v2_assessor import (
    FrozenF9V2ExactContentAssessmentTemplate,
)
from aletheia.observations.scientific_bridge import (
    BridgeValidationDisposition,
    RawRunEnvelope,
    ExternalRawRunEnvelope,
)
from aletheia.research_controller.external_rpc import CuprateDiagnosticResult
from aletheia.research_kernel.schemas import canonical_json_bytes, canonical_sha256

_KNOWN_BINS = frozenset(
    {
        FAMILY_EXCESS_CI_EXCLUDES_ZERO,
        FAMILY_EXCESS_CI_INCLUDES_ZERO,
        ERROR_CONCENTRATES_AT_EXTREMES,
        NO_STRATIFICATION_STRUCTURE,
    }
)


def diagnostic_observation_bytes(result: CuprateDiagnosticResult) -> bytes:
    """Canonical bytes of one diagnostic result as admitted for observation."""

    return canonical_json_bytes(result)


def combined_outcome_bin_id(result: CuprateDiagnosticResult) -> str:
    """Mechanical combined bin id over the two preregistered outcome bins.

    The id must satisfy the bridge's local-id pattern: it is compared for
    membership against the admission policy's frozen outcome-bin mappings,
    whose ids that pattern constrains (contradiction #19 — the earlier
    colon-bearing form could never be a member of any constructible policy).
    """

    d1 = result.d1_matched_control.outcome_bin
    d2 = result.d2_doping_stratification.outcome_bin
    if d1 not in _KNOWN_BINS or d2 not in _KNOWN_BINS:
        raise ValueError("cuprate diagnostic result carries an unknown outcome bin")
    return f"cuprate.d1-{d1}.d2-{d2}"


def diagnostic_assessment_template(
    *,
    raw_run: RawRunEnvelope | ExternalRawRunEnvelope,
    result: CuprateDiagnosticResult,
    disposition: BridgeValidationDisposition,
    blocker_codes: tuple[str, ...] = (),
) -> FrozenF9V2ExactContentAssessmentTemplate:
    """Author one exact-content assessment for the diagnostic's raw run.

    Fails closed unless the raw run's bound observation artifact carries
    exactly the canonical bytes of ``result`` — the author certifies the
    content they are pinning, not a nearby envelope.
    """

    authorization = raw_run.scientific_authorization.message
    binding = authorization.scientific_observation_artifact_binding
    expected_sha = canonical_sha256(result)
    entries = tuple(
        item
        for item in raw_run.artifact_manifest.entries
        if item.artifact_key == binding.artifact_key
    )
    if len(entries) != 1 or entries[0].content_sha256 != expected_sha:
        raise ValueError("raw run does not carry the canonical diagnostic result bytes")
    return FrozenF9V2ExactContentAssessmentTemplate.from_raw_run(
        raw_run=raw_run,
        disposition=disposition,
        outcome_bin_id=combined_outcome_bin_id(result),
        blocker_codes=blocker_codes,
    )
