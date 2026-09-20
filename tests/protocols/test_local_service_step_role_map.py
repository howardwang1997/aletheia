"""The verifier's local-service step-role bridge (contradiction #17).

A local bridge service contract's behavior.role ("analysis") and the
protocol step role a compiled protocol must assign (SCIENTIFIC_EXECUTOR)
are different vocabularies; the pinned verifier bridges them with
_EXPECTED_LOCAL_SERVICE_ROLES. Typecheck (typecheck.py:627-635)
unconditionally requires a step of each of the three roles, so the map
must cover exactly that set — otherwise no protocol can both compile and
source-verify, the state that blocked PAUSE-2 in the 2f-q7 dry run.
"""

from types import SimpleNamespace as NS

import pytest

from aletheia.execution import capability_sources as closure
from tests.protocols.test_capability_sources import verify


def test_step_role_map_covers_the_typecheck_required_roles() -> None:
    expected_roles = {
        closure.expected_local_service_step_role(operation)
        for operation in closure._EXPECTED_LOCAL_SERVICE_ROLES
    }

    # typecheck.py:627-635: SCIENTIFIC_EXECUTOR + OBSERVATION_PARSER +
    # INDEPENDENT_VALIDATOR must each appear among a protocol's steps
    assert expected_roles == {
        "scientific_executor",
        "observation_parser",
        "independent_validator",
    }


@pytest.mark.parametrize(
    "behavior_role, step_role",
    sorted(closure._EXPECTED_LOCAL_SERVICE_ROLES.values()),
)
def test_step_role_map_pairs_differ_only_where_the_dag_demands(behavior_role, step_role):
    """load_raw_run and the campaign service serve their behavior role
    directly; only the cuprate diagnostic crosses vocabularies — its
    behavior role stays "analysis" while its step serves as the
    SCIENTIFIC_EXECUTOR the archive-input gate requires.
    """

    # the crossing, pinned to its operation: swapping cuprate's pair with
    # another operation's passes both the per-pair loop and the coverage
    # set — only this pin (or the verifier's live-contract check) catches it
    assert closure._EXPECTED_LOCAL_SERVICE_ROLES["run_cuprate_diagnostic"] == (
        "analysis",
        "scientific_executor",
    )
    if behavior_role == "analysis":
        assert step_role == "scientific_executor"
    else:
        assert behavior_role == step_role


@pytest.mark.parametrize("case", ["run_cuprate_diagnostic"], indirect=True)
def test_cuprate_step_rejects_the_contract_own_role_string(case):
    """Regression for the pre-fix tautology: feeding the contract's own
    behavior.role back as the step role used to satisfy the verifier; now
    only the mapped executor role verifies.
    """

    step = case.request.protocol.steps[0]
    assert step.role.value == "scientific_executor"
    step.role = NS(value="analysis")
    with pytest.raises(closure.CapabilitySourceVerificationError, match="role"):
        verify(case)
