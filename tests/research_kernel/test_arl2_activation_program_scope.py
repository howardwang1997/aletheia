"""The activation freezes a program scope every kit consumer actually names.

verify_launch_audit refuses a controller launch whose program_id differs from
the quest stream's frozen scope binding (research_controller/launch.py), the
launch request's program_id is a required field, and the deployed signing
authorities compare proposals against their assignment's scope binding with
exact equality. Before the program-id authoring, the authorities script froze
scope bindings without a program_id while the campaign-request script derived
a synthetic one, so the first driver apply died with "controller launch
belongs to another Program". This pins the fixed contract end to end: every
pre-signed activation command, the activation state, the committed stream's
frozen scope, the staged campaign request's launch pin, and the deployments
loader's activation/request facts all carry one derived program_id, and both
downstream scripts refuse a program-less or disagreeing activation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aletheia.config import get_settings
from aletheia.db import create_all
from aletheia.research_kernel.policy import (
    ResearchAuthorizationPolicyV1,
    ResearchAuthorizationTrustRootV1,
)
from aletheia.research_kernel.schemas import canonical_json_bytes
from aletheia.research_store.cas import FilesystemResearchArchive
from aletheia.research_store.store import ResearchKernelStore

_REPO = Path(__file__).resolve().parents[2]
_AUTHORITIES = _REPO / "scripts" / "author-arl2-authorities.py"
_CAMPAIGN_REQUEST = _REPO / "scripts" / "author-arl2-campaign-request.py"
_DEPLOYMENTS = _REPO / "scripts" / "author-arl2-deployments.py"

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

_CSV_TEXT = (
    "material,critical_temp,feature_a\n"
    "Ba2Sr1Cu2O6,90.0,1\n"
    "Ba2Sr1Cu2O6,92.0,1\n"
    "Sr2Ca1Cu2O6,85.0,2\n"
    "LaCuO3,10.0,3\n"
    "Fe2O3,1.5,4\n"
    "Fe2O3,1.7,4\n"
    "SiO2,0.5,5\n"
)


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_spec(working_root: Path, *, label: str) -> Path:
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
        "quest_label": label,
        "mission": "pin the activation program-scope contract",
        # the certified policy refuses a principal spanning two roles, so
        # each role gets its own fixture principal
        "principals": {
            "commissioning": "human:test-commissioning",
            "ordinary": "human:test-ordinary",
            "amendment": "human:test-amendment",
            "emergency": "human:test-emergency",
        },
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


def _run_authorities(tmp_path: Path, monkeypatch, *, label: str) -> tuple[object, Path, dict]:
    create_all()
    module = _load_script(
        "author_arl2_authorities_under_test", _AUTHORITIES
    )
    working_root = tmp_path / "window"
    (working_root / "configs").mkdir(parents=True)
    cas_root = tmp_path / "cas"
    spec_path = _write_spec(working_root, label=label)
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
    return module, working_root, state


def _fixture_card():
    """A verifying card whose split policy leaves both rounds non-empty."""

    from aletheia.protocols.data_registration import (
        DatasetColumnManifestV1,
        DatasetLicenseStatus,
        DatasetPreprocessingPolicyV1,
        DatasetRoundSplitPolicyV1,
        DatasetSourceLineageV1,
        DatasetStratumRuleV1,
        RegisteredDatasetV1,
        enumerate_formula_groups,
        partition_group_rounds,
        recompute_dataset_audit,
    )

    csv_bytes = _CSV_TEXT.encode("utf-8")
    manifest = DatasetColumnManifestV1(
        columns=("critical_temp", "feature_a", "material"),
        composition_column="material",
        target_column="critical_temp",
        feature_columns=("feature_a",),
    )
    content_sha = hashlib.sha256(csv_bytes).hexdigest()
    groups = enumerate_formula_groups(_CSV_TEXT, manifest)
    for index in range(40):
        policy = DatasetRoundSplitPolicyV1(
            holdout_percent=50, salt=f"arl2-program-scope-{index:04d}"
        )
        round_one, round_two = partition_group_rounds(content_sha, policy, groups)
        if len(round_one) >= 2 and len(round_two) >= 1:
            break
    else:
        raise AssertionError("no fixture salt splits the five formula groups")
    lineage = DatasetSourceLineageV1(
        name="synthetic program-scope fixture",
        url="https://example.invalid/synthetic.csv",
        retrieval_note="authored in-test",
        retrieved_at=datetime(2026, 9, 19, tzinfo=timezone.utc),
        raw_filename="synthetic.csv",
        raw_sha256=content_sha,
        raw_bytes=len(csv_bytes),
        citation="synthetic fixture; no external source",
        license_terms="CC0 (synthetic)",
        license_status=DatasetLicenseStatus.VERIFIED,
    )
    return RegisteredDatasetV1(
        dataset_id="synthetic-program-scope-fixture",
        version=1,
        lineage=lineage,
        content_sha256=content_sha,
        row_count=7,
        column_manifest=manifest,
        preprocessing=DatasetPreprocessingPolicyV1(
            policy_version="1.0.0",
            statements=("no row-level preprocessing; registered content is the retrieved file",),
        ),
        audit_verdicts=recompute_dataset_audit(
            content_sha256=content_sha,
            column_manifest=manifest,
            stratum_rules=(
                DatasetStratumRuleV1(
                    stratum_id="multi_ae_cuprate",
                    required_elements=("Cu", "O"),
                    multi_choice_elements=("Ba", "Ca", "Mg", "Sr"),
                    multi_choice_minimum=2,
                ),
            ),
            csv_text=_CSV_TEXT,
        ),
        stratum_rules=(
            DatasetStratumRuleV1(
                stratum_id="multi_ae_cuprate",
                required_elements=("Cu", "O"),
                multi_choice_elements=("Ba", "Ca", "Mg", "Sr"),
                multi_choice_minimum=2,
            ),
        ),
        split_policy=policy,
        limitations=("synthetic fixture for the program-scope contract",),
    )


def _write_request_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    card = _fixture_card()
    card_path = tmp_path / "fixture-card.json"
    # the script re-stages canonically and pins the staged sha against the
    # card's own dataset_card_sha256, so the fixture file is the canonical body
    card_path.write_bytes(canonical_json_bytes(card))
    content_path = tmp_path / "fixture-content.csv"
    content_path.write_text(_CSV_TEXT)
    actions_path = tmp_path / "fixture-staged-actions.json"
    actions_path.write_text(
        json.dumps(
            {
                "1": {
                    "action_sha256": hashlib.sha256(b"round-one-action").hexdigest(),
                    "proposal_request_sha256": hashlib.sha256(
                        b"round-one-request"
                    ).hexdigest(),
                    "batch_max_rows": None,
                },
                "2": {
                    "action_sha256": hashlib.sha256(b"round-two-action").hexdigest(),
                    "proposal_request_sha256": hashlib.sha256(
                        b"round-two-request"
                    ).hexdigest(),
                    "batch_max_rows": None,
                },
            }
        )
    )
    return card_path, content_path, actions_path


def _run_campaign_request(
    monkeypatch,
    *,
    working_root: Path,
    activation_state: Path,
    card_path: Path,
    content_path: Path,
    actions_path: Path,
) -> int:
    module = _load_script(
        "author_arl2_campaign_request_under_test", _CAMPAIGN_REQUEST
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "author-arl2-campaign-request.py",
            "--working-root",
            str(working_root),
            "--database-url",
            get_settings().database_url,
            "--card",
            str(card_path),
            "--content",
            str(content_path),
            "--staged-actions",
            str(actions_path),
            "--activation-state",
            str(activation_state),
        ],
    )
    return module.main()


def test_activation_freezes_one_program_scope_across_state_commands_and_stream(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module, working_root, state = _run_authorities(
        tmp_path, monkeypatch, label="arl2-program-scope-test"
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


def test_campaign_request_and_deployments_name_the_frozen_program_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from aletheia.arl2_runtime import ARL2QuestionCampaignRequestV1

    _module, working_root, state = _run_authorities(
        tmp_path, monkeypatch, label="arl2-program-scope-request-test"
    )
    program_id = state["program_id"]
    card_path, content_path, actions_path = _write_request_inputs(tmp_path)
    activation_state = working_root / "configs" / "arl2-activation-state.json"

    assert (
        _run_campaign_request(
            monkeypatch,
            working_root=working_root,
            activation_state=activation_state,
            card_path=card_path,
            content_path=content_path,
            actions_path=actions_path,
        )
        == 0
    )

    request_state = json.loads(
        (working_root / "configs" / "arl2-request-state.json").read_text()
    )
    staged = ARL2QuestionCampaignRequestV1.model_validate_json(
        Path(request_state["request_path"]).read_bytes()
    )
    assert staged.launch_request.program_id == program_id
    assert staged.quest_id == state["quest_id"]

    deployments = _load_script(
        "author_arl2_deployments_under_test", _DEPLOYMENTS
    )
    activation_facts = deployments._load_activation_inputs(str(activation_state))
    assert activation_facts["program_id"] == program_id
    request_facts = deployments._load_request_inputs(
        str(working_root / "configs" / "arl2-request-state.json")
    )
    assert request_facts["program_id"] == program_id
    # the assignments frozen into the deployed signing authorities bind the
    # same program scope (they compare proposals with exact equality)
    controller_assignment, observation_assignment = (
        deployments._kernel_policy_assignments(activation_facts)
    )
    for assignment in (controller_assignment, observation_assignment):
        assert assignment.quest_id == state["quest_id"]
        assert assignment.scope_binding.program_id == program_id


def test_programless_or_disagreeing_activations_are_refused(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import pytest

    _module, working_root, state = _run_authorities(
        tmp_path, monkeypatch, label="arl2-program-scope-negative-test"
    )
    card_path, content_path, actions_path = _write_request_inputs(tmp_path)

    # a second working root: the first now carries a request state, which the
    # request script refuses to overwrite
    negative_root = tmp_path / "window-negative"
    (negative_root / "configs").mkdir(parents=True)

    programless = dict(state)
    programless.pop("program_id")
    programless_path = tmp_path / "doctored-programless-state.json"
    programless_path.write_text(json.dumps(programless))
    with pytest.raises(SystemExit, match="carries no program scope"):
        _run_campaign_request(
            monkeypatch,
            working_root=negative_root,
            activation_state=programless_path,
            card_path=card_path,
            content_path=content_path,
            actions_path=actions_path,
        )

    disagreeing = dict(state)
    disagreeing["program_id"] = "prg_" + "1" * 32
    disagreeing_path = tmp_path / "doctored-disagreeing-state.json"
    disagreeing_path.write_text(json.dumps(disagreeing))
    with pytest.raises(SystemExit, match="store stream scope program differs"):
        _run_campaign_request(
            monkeypatch,
            working_root=negative_root,
            activation_state=disagreeing_path,
            card_path=card_path,
            content_path=content_path,
            actions_path=actions_path,
        )

    deployments = _load_script(
        "author_arl2_deployments_under_test", _DEPLOYMENTS
    )
    with pytest.raises(SystemExit, match="carries no program scope"):
        deployments._load_activation_inputs(str(programless_path))
