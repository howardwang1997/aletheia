"""Kit-side coverage for the catalog script's authored resource class.

`scripts/author-arl2-capability-catalog.py` authors the three bridge
capability manifests (runtime_kind external_service) alongside the
campaign's single static resource class. The compile gate (typecheck.py
_check_capability_and_resource) only accepts external-kind classes whose
external_action_kinds carry the capability's exact action kind for such
steps — the 2f-q7 dry run commissioned a cpu class and no protocol could
ever compile against it (contradiction #13); the triad extension
(contradiction #16) made one class serve all three operations, which the
gate's membership predicate admits. The script has no package, so this
file loads it directly and checks the authored class against the gate's
condition.
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


def test_authored_resource_class_serves_the_external_service_capability() -> None:
    from aletheia.execution.schemas import NetworkPolicy, ResourceKind, StaticResourceClass

    module = _script_module()
    authored = module._resource_class_from_facts(
        {"uname": "Linux 6.8.0 v100ts x86_64"},
        16,
        33610205824,
        14110695424,
    )
    resource_class = StaticResourceClass.model_validate(authored)

    # the external-runtime branch of the compile gate: a class serving an
    # external_service capability must be external-kind and carry the
    # capability's exact action kind; the predicate is membership, so one
    # class carrying all three triad action kinds serves every step
    assert resource_class.kind is ResourceKind.EXTERNAL
    assert set(resource_class.external_action_kinds) == set(module._OPERATIONS)
    # the cuprate manifest pins network_egress "none", so the step request
    # resolves to NetworkPolicy.NONE and the class must offer it
    assert NetworkPolicy.NONE in resource_class.network_policies
    # capacities survive the re-kinding: they bound the step's structural
    # request through _resource_class_matches
    assert resource_class.cpu_cores == 16
