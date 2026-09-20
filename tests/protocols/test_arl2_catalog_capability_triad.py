"""Kit-side coverage for the catalog script's capability triad.

`scripts/author-arl2-capability-catalog.py` freezes one manifest per
operation in _OPERATIONS. Typecheck requires a three-role protocol with
four distinct independence groups and disjoint per-role principal sets
(contradiction #16), and every manifest declares all four groups so a
step of any role sees the other roles' groups inside
required_independence_groups. The structural preconditions the protocol
layer depends on are checked here against the script's own tables, and
the authored runtime blocks are pinned against the source verifier's
hardcoded per-operation clauses; the script has no package, so this
file loads it directly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "author-arl2-capability-catalog.py"


def _script_module():
    spec = importlib.util.spec_from_file_location("author_arl2_capability_catalog", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_triad_covers_the_kit_required_three_roles() -> None:
    """The kit's three local service operations, one per required role:
    the parser consumes the slot registration, the executor runs the
    diagnostic, the validator assesses the raw run.
    """

    module = _script_module()

    assert set(module._OPERATIONS) == {
        "load_raw_run",
        "run_cuprate_diagnostic",
        "prepare_validation_campaign",
    }
    roles = {item["role"] for item in module._OPERATIONS.values()}
    assert roles == {"observation_parser", "analysis", "independent_validator"}


def test_triad_principals_are_pairwise_distinct() -> None:
    """The step-level independence gate needs disjoint per-role principal
    sets, so no two triad capabilities may deploy under one principal.
    """

    module = _script_module()

    principals = [item["executor_principal_id"] for item in module._OPERATIONS.values()]
    assert len(principals) == len(set(principals))
    # the deployed bridge pins these exact service/bridge principal ids
    assert (
        module._OPERATIONS["run_cuprate_diagnostic"]["executor_principal_id"]
        == "service.arl2.cuprate-diagnostic"
    )
    assert (
        module._OPERATIONS["load_raw_run"]["executor_principal_id"] == "service.arl2.raw-run-source"
    )
    assert (
        module._OPERATIONS["prepare_validation_campaign"]["executor_principal_id"]
        == "principal.arl2.bridge.observation-validator"
    )


def test_every_manifest_declares_all_four_independence_groups() -> None:
    """Four distinct groups are required; each manifest carries all four so
    any step's other-role principals satisfy the group-containment clause.
    """

    module = _script_module()

    groups = list(module._INDEPENDENCE_GROUPS)
    assert len(set(groups)) == 4
    # the manifest validator requires canonical (sorted) order
    assert groups == sorted(groups)
    # the manifests carry the list itself — the principal block's wiring
    source = _SCRIPT_PATH.read_text()
    assert '"required_independence_groups": list(_INDEPENDENCE_GROUPS)' in source


def test_runtime_blocks_match_the_pinned_verifier_clauses() -> None:
    """capability_sources.py _verify_local_service_sources hardcodes these
    per-operation expectations: determinism frozen_seeds with seeds [0] on
    the cuprate diagnostic and declared_stochastic elsewhere, no
    checkpointing, and reconciliation only on the campaign service. A drift
    here passes authoring and kills the real run at PAUSE-2.
    """

    module = _script_module()

    for operation in module._OPERATIONS:
        runtime = module._runtime_block(
            operation,
            {"runtime_kind": "external_service", "adapter_ref": "adapter.probe"},
            "impl-sha",
            "env-sha",
        )
        if operation == "run_cuprate_diagnostic":
            assert runtime["determinism"] == "frozen_seeds"
            assert runtime["frozen_seeds"] == [0]
        else:
            assert runtime["determinism"] == "declared_stochastic"
            assert runtime["frozen_seeds"] == []
        assert runtime["checkpoint_supported"] is False
        assert runtime["reconciliation_supported"] == (operation == "prepare_validation_campaign")
        assert (
            runtime["maximum_wall_time_seconds"]
            == module._OPERATIONS[operation]["wall_time_seconds"]
        )
