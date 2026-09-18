"""The activation freezes a program scope the launch request can actually name.

verify_launch_audit refuses a controller launch whose program_id differs from
the quest stream's frozen scope binding (research_controller/launch.py), and
the launch request's program_id is a required field. Before the program-id
authoring, the authorities script froze scope bindings without a program_id
while the campaign-request script derived a synthetic one, so the first driver
apply died with "controller launch belongs to another Program". This pins the
fixed contract: every pre-signed activation command, the activation state, and
the committed stream's frozen scope all carry one derived program_id.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from aletheia.config import get_settings
from aletheia.db import create_all
from aletheia.research_kernel.policy import (
    ResearchAuthorizationPolicyV1,
    ResearchAuthorizationTrustRootV1,
)
from aletheia.research_store.cas import FilesystemResearchArchive
from aletheia.research_store.store import ResearchKernelStore

_REPO = Path(__file__).resolve().parents[2]
_AUTHORITIES = _REPO / "scripts" / "author-arl2-authorities.py"

_POLICY_DOCUMENTS = (
    "safety",
    "ethics",
    "license",
    "privacy",
    "egress",
    "budget",
    "approval",
    "publication",
)


def _load_authorities_module():
    spec = importlib.util.spec_from_file_location(
        "author_arl2_authorities_under_test", _AUTHORITIES
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_spec(working_root: Path) -> Path:
    documents = working_root / "policy-documents"
    documents.mkdir(parents=True)
    for name in _POLICY_DOCUMENTS:
        (documents / f"{name}.md").write_text(f"{name} policy stub\n")
    grounding = working_root / "grounding"
    grounding.mkdir()
    grounding_paths = []
    for index, name in enumerate(("card.json", "manifest.json", "fixation.md")):
        target = grounding / name
        target.write_text(f"grounding stub {index}\n")
        grounding_paths.append(str(target))
    spec = {
        "schema_name": "aletheia.arl2_activation_spec",
        "schema_version": 1,
        "quest_label": "arl2-program-scope-test",
        "mission": "pin the activation program-scope contract",
        "principals": {},
        "policy_documents": {
            name: str(documents / f"{name}.md") for name in _POLICY_DOCUMENTS
        },
        "problem": {
            "title": "test problem",
            "statement": "test problem statement",
            "scope": "test scope",
            "importance_rationale": "test rationale",
        },
        "question": {
            "kind": "comparative",
            "statement": "test problem statement",
            "scope": "test scope",
            "answer_space": ["a", "b"],
            "scientific_value": "test value",
            "falsifiability": "test falsifiability",
            "grounding_object_paths": grounding_paths,
        },
    }
    spec_path = working_root / "activation-spec.json"
    spec_path.write_text(json.dumps(spec, indent=1))
    return spec_path


def test_activation_freezes_one_program_scope_across_state_commands_and_stream(
    tmp_path: Path,
    monkeypatch,
) -> None:
    create_all()
    module = _load_authorities_module()

    working_root = tmp_path / "window"
    (working_root / "configs").mkdir(parents=True)
    cas_root = tmp_path / "cas"
    spec_path = _write_spec(working_root)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "author-arl2-authorities.py",
            "--working-root",
            str(working_root),
            "--cas-root",
            str(cas_root),
            "--database-url",
            get_settings().database_url,
            "--spec",
            str(spec_path),
        ],
    )
    assert module.main() == 0

    state = json.loads(
        (working_root / "configs" / "arl2-activation-state.json").read_text()
    )
    quest_id = state["quest_id"]
    program_id = state["program_id"]

    assert program_id == module.derived_program_id(quest_id)
    assert len(program_id) == 36 and program_id.startswith("prg_")
    assert int(program_id[4:], 16) >= 0  # the tail is lowercase hex

    spool = working_root / "spool" / "activation"
    for name in ("charter", "problem", "question"):
        command = json.loads((spool / f"{name}.json").read_text())
        assert command["scope_binding"]["program_id"] == program_id, name

    authority = json.loads(Path(state["authority_manifest_path"]).read_text())
    trust_root = ResearchAuthorizationTrustRootV1.model_validate(
        authority["trust_root"]
    )
    policy = ResearchAuthorizationPolicyV1.model_validate(authority["policy"])
    archive = FilesystemResearchArchive(
        Path(authority["cas_root"]),
        max_object_bytes=64 * 1024 * 1024,
        read_only=False,
        directory_mode=0o700,
        object_mode=0o400,
    )
    store = ResearchKernelStore(
        trust_root=trust_root, archive=archive, genesis_policy=policy
    )
    audit = store.audit(quest_id)
    assert len(audit.events) == 3
    assert audit.scope_binding.program_id == program_id
