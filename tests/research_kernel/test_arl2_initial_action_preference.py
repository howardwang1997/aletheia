"""The authored initial action preference puts DISCRIMINATE first.

The deterministic proposal provider walks the policy's
initial_action_kind_preference in order at every initial PROPOSE_ACTION
(initial requests cannot carry required_action_kind) and every ACTIVE
branch allows all active kinds, so preference[0] decides the initial
action kind. Authored in plain ActionKind enum order the head was
CONTINUE, while the compilation policy admits DISCRIMINATE only and both
staged campaign rounds are discriminate executions — every fresh quest
proposed continue and the round-1 provider template refused with "action
... is continue; ARL-2 pins DISCRIMINATE" (runbook contradiction #8,
2026-09-19). These tests pin the authored order and prove the real
selection function produces the discriminate initial action under it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from aletheia.research_controller.action_proposal_provider import _selection
from aletheia.research_kernel.schemas import ActionKind

_REPO = Path(__file__).resolve().parents[2]
_DEPLOYMENTS = _REPO / "scripts" / "author-arl2-deployments.py"


def _load_deployments():
    spec = importlib.util.spec_from_file_location(
        "author_arl2_deployments_preference_test", _DEPLOYMENTS
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_preference_pins_discriminate_first_over_the_full_set() -> None:
    preference = _load_deployments()._initial_action_kind_preference()

    assert preference[0] is ActionKind.DISCRIMINATE
    # the pin validator requires the unique full set minus ACTIVATE;
    # only the order is a campaign decision
    assert set(preference) == {
        kind for kind in ActionKind if kind is not ActionKind.ACTIVATE
    }
    assert len(preference) == len(set(preference))


def test_authored_preference_selects_the_discriminate_initial_action() -> None:
    deployments = _load_deployments()
    preference = deployments._initial_action_kind_preference()

    # an ACTIVE branch allows every kind (action_proposal_service
    # _initial_targets), so the preference head wins the initial proposal
    request = SimpleNamespace(
        required_action_kind=None,
        targets=(SimpleNamespace(allowed_action_kinds=tuple(ActionKind)),),
    )
    _target, kind = _selection(request, SimpleNamespace(
        initial_action_kind_preference=preference
    ))

    assert kind is ActionKind.DISCRIMINATE
