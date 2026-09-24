"""Import-level coverage for the bridge-dispatch driver's lazy composition imports."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import dispatch_arl2_external_execution as driver  # noqa: E402


def test_compose_allocator_imports_resolve_against_the_deployed_package() -> None:
    """Contradiction #22: the composition seam's lazy imports must be executable.

    The dispatch tests patch ``_compose_allocator`` wholesale, so wrong
    module paths inside it (NodeEnrollmentAuthorityVerifier attributed to
    runtime_v2_contracts, TerminalVerificationAuthorityVerifier attributed
    to a nonexistent terminal_verification module — both live in
    runtime_contracts) ship green through CI and only surface on the
    deployed box. Calling the seam with an empty state executes every
    import line before failing on state access, so an ImportError fails
    this test while any state-shaped error passes.
    """

    with pytest.raises(KeyError):
        driver._compose_allocator({}, Path("/nonexistent-state.json"), None)
