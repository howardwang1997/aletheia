"""Author one F9-v2 exact-content assessment template for a cuprate diagnostic raw run.

Reads the commissioning dry run's raw-run envelope and diagnostic result
from files, cross-checks that the envelope carries exactly the result's
canonical bytes, and prints the authored template plus its sha256 for PI
review.  The disposition and blocker codes are inputs: they come from the
preregistered AnalysisPlan rule, never from the run's numbers.  Live
authoring needs the quest harness's context ids, so this runs at the
commissioning dry run, not in tests.

    conda run -n aletheia python scripts/author_cuprate_exact_content.py \
        --raw-run raw_run.json --result diagnostic_result.json \
        --disposition validated_confirmation
"""

from __future__ import annotations

import argparse
import json

from aletheia.execution.cuprate.exact_content import (
    combined_outcome_bin_id,
    diagnostic_assessment_template,
)
from aletheia.observations.scientific_bridge import (
    BridgeValidationDisposition,
    parse_raw_run_envelope,
)
from aletheia.research_controller.external_rpc import CuprateDiagnosticResult
from aletheia.research_kernel.schemas import canonical_json_bytes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-run", required=True, help="path to the raw run envelope JSON")
    parser.add_argument("--result", required=True, help="path to the diagnostic result JSON")
    parser.add_argument(
        "--disposition",
        required=True,
        choices=[item.value for item in BridgeValidationDisposition],
    )
    parser.add_argument(
        "--blocker",
        action="append",
        default=[],
        help="blocker code (repeatable); required for rejected dispositions",
    )
    args = parser.parse_args(argv)

    with open(args.raw_run, "rb") as handle:
        raw_run = parse_raw_run_envelope(json.loads(handle.read()))
    with open(args.result, "rb") as handle:
        result = CuprateDiagnosticResult.model_validate_json(handle.read())

    template = diagnostic_assessment_template(
        raw_run=raw_run,
        result=result,
        disposition=BridgeValidationDisposition(args.disposition),
        blocker_codes=tuple(sorted(set(args.blocker))),
    )
    print(
        "TEMPLATE_JSON "
        + json.dumps(
            {
                "template_sha256": template.template_sha256,
                "lookup_sha256": template.lookup_sha256,
                "outcome_bin_id": combined_outcome_bin_id(result),
                "template": json.loads(canonical_json_bytes(template)),
            },
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
