"""Kit-side coverage for the catalog script's authored claim ceiling.

`scripts/author-arl2-capability-catalog.py` freezes the cuprate capability
manifest's ClaimCeiling. The schema default for
independent_validation_required is True, and a true value forces an
INDEPENDENT_VALIDATOR step that consumes the observable's output port —
unsatisfiable against this single-capability catalog, because a step port
must exist in its capability's interface and no second capability carries
the diagnostic output as an input (2f-q7 dry run, contradiction #14).
The ceiling is therefore authored explicitly. The script has no package,
so this file loads it directly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "author-arl2-capability-catalog.py"


def _script_module():
    spec = importlib.util.spec_from_file_location(
        "author_arl2_capability_catalog", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_authored_claim_ceiling_is_explicit_about_independent_validation() -> None:
    """PI decision 2026-09-20: this dry run's independent validation rides
    the observation bridge (independent-validation service plus the bridge
    observation-validator/admitter principals); the ceiling is authored
    False with the rationale recording where validation lives, instead of
    silently inheriting the True schema default.
    """

    ceiling = _script_module()._claim_ceiling()

    assert ceiling.independent_validation_required is False
    assert "observation bridge" in ceiling.rationale
    assert ceiling.required_replication_tier.value == "exact_reexecution"
