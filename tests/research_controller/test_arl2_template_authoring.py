"""Kit-side coverage for the merged round-split authoring helper.

The provider-mode commissioning script runs only inside the ARL-2 window;
its merged channel (PI decision Q13(b)) derives the five round-split values
that feed the protocol template from the byte-pinned campaign request. The
script has no package, so this file loads it directly and drives the
value-derivation helper through the selection and row-count cases the
deployed compile gate cannot discriminate on its own.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "author_arl2_sea_and_provider_templates.py"


def _script_module():
    spec = importlib.util.spec_from_file_location(
        "author_arl2_sea_and_provider_templates", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _binding(round_index: int, *, tag: str, rows: int = 1):
    from aletheia.research_controller.protocol_compilation_step import (
        RoundSplitBindingPolicyV1,
        RoundSplitTemplateBindingV1,
    )

    # extra rows differ in every pinned sha: the binding model itself
    # rejects duplicate rows, and this file targets the script-side
    # row-COUNT guard, which needs a model-valid multi-row binding
    def row(offset: int) -> RoundSplitTemplateBindingV1:
        suffix = "" if offset == 0 else f"-{offset}"
        return RoundSplitTemplateBindingV1(
            action_sha256=_sha(f"{tag}{suffix}-action"),
            bound_batch_group_ids_sha256=_sha(f"{tag}{suffix}-batch"),
            spent_group_ids_sha256=_sha(f"{tag}{suffix}-spent"),
            unspent_group_ids_sha256=_sha(f"{tag}{suffix}-unspent"),
        )

    # the binding model also requires rows ordered by action sha
    multiple = sorted((row(offset) for offset in range(rows)), key=lambda item: item.action_sha256)
    return RoundSplitBindingPolicyV1(
        dataset_content_sha256=_sha("registered-content"),
        split_policy_sha256=_sha("split-policy"),
        round_index=round_index,
        sealed_group_ids_sha256=_sha(f"{tag}-groups"),
        template_bindings=tuple(multiple),
    )


def _document(*bindings) -> dict:
    return {
        "schema_name": "aletheia.arl2_question_campaign_request",
        "round_split_bindings": [item.model_dump(mode="json") for item in bindings],
    }


def _expected_values(binding) -> dict:
    row = binding.template_bindings[0]
    return {
        "dataset_content_sha256": binding.dataset_content_sha256,
        "round_bound_batch_group_ids": row.bound_batch_group_ids_sha256,
        "round_sealed_group_ids": binding.sealed_group_ids_sha256,
        "round_spent_group_ids": row.spent_group_ids_sha256,
        "round_unspent_group_ids": row.unspent_group_ids_sha256,
    }


def test_merged_round_split_values_derive_each_round_from_its_own_binding() -> None:
    module = _script_module()
    first = _binding(1, tag="round-one")
    second = _binding(2, tag="round-two")
    document = _document(first, second)

    assert module._merged_round_split_values(document, round_index=1) == (
        _expected_values(first)
    )
    # the round-2 derivation must not silently reuse the first binding
    assert module._merged_round_split_values(document, round_index=2) == (
        _expected_values(second)
    )


def test_merged_round_split_values_require_the_named_round(capsys) -> None:
    module = _script_module()
    document = _document(_binding(1, tag="round-one"))
    with pytest.raises(SystemExit):
        module._merged_round_split_values(document, round_index=2)
    # _fail prints to stderr (SystemExit carries no message), so the
    # guard's own text is the discriminator between failure modes
    assert "no unique round 2 binding" in capsys.readouterr().err


def test_merged_round_split_values_require_exactly_one_template_row(capsys) -> None:
    module = _script_module()
    document = _document(_binding(1, tag="round-one", rows=2))
    with pytest.raises(SystemExit):
        module._merged_round_split_values(document, round_index=1)
    assert "exactly one template row" in capsys.readouterr().err


def test_merged_round_split_values_require_readable_bindings(capsys) -> None:
    module = _script_module()
    with pytest.raises(SystemExit):
        module._merged_round_split_values({}, round_index=1)
    assert "readable round_split_bindings" in capsys.readouterr().err


def test_provider_refusal_formatter_reads_the_real_blocker_field() -> None:
    """The dirty-compile refusal join must read ProtocolBlocker.code.

    The 2f-q7 dry run hit a dirty compile whose refusal crashed inside the
    formatter itself (`blocker_code` does not exist on ProtocolBlocker),
    masking the actual blockers behind an AttributeError. The formatter
    lives mid-_run_provider behind a full commissioning harness, so this
    check executes the shipped join expression itself against a real
    blocker — the same source-scrape idiom the migration inventory uses.
    """

    from aletheia.protocols.schemas import ProtocolBlocker, ProtocolBlockerCode

    blocker = ProtocolBlocker.model_validate(
        {
            "code": ProtocolBlockerCode.CAPABILITY_UNAVAILABLE.value,
            "location": "steps[0].capability_requirement",
            "subject_id": "requirement.step.01",
            "detail": "no frozen capability manifest satisfies the exact selector",
        }
    )
    join_lines = [
        line
        for line in _SCRIPT_PATH.read_text().splitlines()
        if "result.report.blockers)" in line and "join(" in line
    ]
    assert len(join_lines) == 1, "expected exactly one blocker-join line to scrape"
    join_line = join_lines[0]
    from types import SimpleNamespace

    result = SimpleNamespace(report=SimpleNamespace(blockers=(blocker,)))
    expression = compile(join_line.split("=", 1)[1].strip(), "<formatter>", "eval")
    formatted = eval(expression, {"result": result})
    assert formatted == "capability_unavailable"


def test_protocol_input_media_type_settles_each_supported_kind() -> None:
    """Contradiction #18 field sourcing: the kind→media map must be total.

    Only the four byte-oriented kinds carry a canonical admission media type;
    every other ArtifactKind (model, sample, measurement, proof, receipt) has
    no operator-staged byte form and must fail loudly instead of guessing.
    """

    from aletheia.protocols.capabilities import ArtifactKind

    module = _script_module()
    for kind, expected in (
        ("json", "application/json"),
        ("table", "text/csv"),
        ("text", "text/plain"),
        ("binary", "application/octet-stream"),
    ):
        # both the enum member and the raw wire string reach the same media type
        assert module._protocol_input_media_type(kind) == expected
        assert module._protocol_input_media_type(ArtifactKind(kind)) == expected


def test_protocol_input_media_type_refuses_unsettleable_kinds(capsys) -> None:
    from aletheia.protocols.capabilities import ArtifactKind

    module = _script_module()
    for kind in (ArtifactKind.MODEL, ArtifactKind.PROOF, "video"):
        with pytest.raises(SystemExit):
            module._protocol_input_media_type(kind)
    assert "must settle on one of" in capsys.readouterr().err


def test_protocol_input_requirement_sources_fields_from_the_port_contract() -> None:
    """The admission ExpectedArtifact must be derived, field for field.

    artifact_key is the port id, role stays RAW_OUTPUT (the qualification
    input check only resolves raw-output receipts), schema_sha256 comes from
    the port's schema ref, data_classification accepts both the enum and its
    wire string, and retention/max_bytes pass through unchanged.
    """

    from types import SimpleNamespace

    from aletheia.execution.schemas import ArtifactRole
    from aletheia.protocols.capabilities import DataClassification

    module = _script_module()
    port = SimpleNamespace(
        port_id="input.dataset_rows",
        artifact_kind="json",
        schema_ref=SimpleNamespace(schema_sha256=_sha("port-schema")),
        data_classification=DataClassification.PUBLIC,
    )
    requirement = module._protocol_input_requirement(
        port=port,
        retention_policy_sha256=_sha("retention"),
        max_bytes=4096,
    )
    assert requirement.artifact_key == "input.dataset_rows"
    assert requirement.role is ArtifactRole.RAW_OUTPUT
    assert requirement.media_type == "application/json"
    assert requirement.schema_sha256 == _sha("port-schema")
    assert requirement.data_classification == DataClassification.PUBLIC
    assert requirement.retention_policy_sha256 == _sha("retention")
    assert requirement.max_bytes == 4096
    # the enum shim: a raw wire string classification survives too
    wire_port = SimpleNamespace(
        port_id="input.dataset_rows",
        artifact_kind="json",
        schema_ref=port.schema_ref,
        data_classification="public",
    )
    assert (
        module._protocol_input_requirement(
            port=wire_port,
            retention_policy_sha256=_sha("retention"),
            max_bytes=4096,
        ).data_classification
        == DataClassification.PUBLIC
    )


def test_produced_output_port_ids_spans_every_work_order_node() -> None:
    """Contradiction #18's input partition keys on ALL producers, not one.

    An executor input fed by a sibling node's output port is a lineage edge
    even when that sibling is not the executor itself; missing a producer
    would silently reclassify a lineage input as protocol-level.
    """

    from types import SimpleNamespace

    module = _script_module()
    work_order = SimpleNamespace(
        nodes=(
            SimpleNamespace(node_id="loader", output_port_ids=("intermediate.groups",)),
            SimpleNamespace(node_id="executor", output_port_ids=()),
            SimpleNamespace(
                node_id="validator", output_port_ids=("validation.report",)
            ),
        )
    )
    assert module._produced_output_port_ids(work_order) == {
        "intermediate.groups",
        "validation.report",
    }
