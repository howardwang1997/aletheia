"""Machine checks for the PR-0 legacy golden compatibility contract.

The contract intentionally freezes named engineering invariants and the source
identities that assert them.  It is not a scientific-result fixture and cannot
be refreshed implicitly from a live run.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any


_ROOT = Path(__file__).resolve().parents[2]
_CONTRACT_PATH = _ROOT / "tests/fixtures/legacy/v1/golden_contract.v1.json"
_RUN_SOURCE_PATH = _ROOT / "tests/fixtures/legacy/v1/run_projection_sources.v1.json"
_CONTRACT_FILE_SHA256 = "cf8e99eea6b30bf89f67749eb8ace2e6db04956e01948530636c2077217b7802"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DOMAINS = {"materials", "molecules", "rag"}
_FULL_CASE_SHA256 = {
    "materials.driver_dry.completed.v1": (
        "3dc394dfed0fee314faf344440fb968b94699ed992dbb48f67fb669586e79840"
    ),
    "materials.plugin.completed.v1": (
        "ad95edbf77fccff0bc655ba2fe852431bc6de88a54b10d607e6dae0c582f18cd"
    ),
    "molecules.plugin.completed.v1": (
        "a60802347f4c84fe5d54eeafc25cb4fe1bf24320c383435d671c28662274eee7"
    ),
    "rag.driver_dry.completed.v1": (
        "052dd10571b72c35b7613463bf37e5e1d56e200d6f0624452b28c3815145ab64"
    ),
    "rag.plugin.completed.v1": ("4367cdc85694d1f3ed1e5251ec561974bcba9cccf10c4926214b40c0409a89c5"),
}


def _load_contract() -> dict[str, Any]:
    payload = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _top_level_test_nodes(path: Path) -> set[str]:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def test_contract_is_explicitly_engineering_only_and_never_live_refreshed() -> None:
    assert hashlib.sha256(_CONTRACT_PATH.read_bytes()).hexdigest() == _CONTRACT_FILE_SHA256
    contract = _load_contract()

    assert contract["schema_name"] == "aletheia.legacy_golden_compatibility_contract"
    assert contract["schema_version"] == 1
    assert contract["contract_id"] == "legacy-golden-compatibility-v1"
    assert contract["contract_status"] == "frozen"
    assert contract["use_policy"] == {
        "claim_ceiling": "engineering_regression_only",
        "scientific_admission": False,
        "model_training": False,
        "live_refresh": False,
        "refresh_policy": "publish_a_new_explicit_contract_version",
    }


def test_frozen_source_identities_resolve_to_named_offline_tests_or_fixtures() -> None:
    contract = _load_contract()
    sources = contract["source_identities"]
    source_ids = [source["source_id"] for source in sources]

    assert source_ids
    assert len(source_ids) == len(set(source_ids))

    for source in sources:
        assert source["kind"] in {"offline_test", "offline_fixture"}
        relative = Path(source["path"])
        assert not relative.is_absolute()
        resolved = (_ROOT / relative).resolve(strict=True)
        assert resolved.is_relative_to(_ROOT.resolve())
        assert resolved.is_file()

        expected_sha = source["sha256"]
        assert _SHA256.fullmatch(expected_sha)
        assert hashlib.sha256(resolved.read_bytes()).hexdigest() == expected_sha

        declared_nodes = source["test_nodes"]
        if source["kind"] == "offline_test":
            assert declared_nodes
            actual_nodes = _top_level_test_nodes(resolved)
            assert set(declared_nodes) <= actual_nodes
        else:
            assert declared_nodes == []


def test_case_identities_and_bindings_are_content_addressed() -> None:
    contract = _load_contract()
    sources = {source["source_id"]: source for source in contract["source_identities"]}
    cases = contract["cases"]
    case_ids = [case["case_id"] for case in cases]

    assert len(case_ids) == len(set(case_ids))
    assert set(case_ids) == set(_FULL_CASE_SHA256)
    assert {case["identity"]["material"]["domain"] for case in cases} == _DOMAINS

    referenced_sources: set[str] = set()
    for case in cases:
        # The embedded identity is a stable locator for the selected source bindings.  This
        # independent test constant covers the *complete* behavior contract, including metrics,
        # events, artifacts, terminal semantics, and invariants.
        assert _canonical_sha256(case) == _FULL_CASE_SHA256[case["case_id"]]
        material = case["identity"]["material"]
        identity_sha = case["identity"]["sha256"]
        assert material["case_id"] == case["case_id"]
        assert material["domain"] in _DOMAINS
        assert material["outcome"] in {"completed", "results_rejected"}
        assert material["source_bindings"] == sorted(material["source_bindings"])
        assert _SHA256.fullmatch(identity_sha)
        assert _canonical_sha256(material) == identity_sha

        binding_source_ids = {binding["source_id"] for binding in case["bindings"]}
        assert binding_source_ids == set(material["source_bindings"])
        for binding in case["bindings"]:
            source = sources[binding["source_id"]]
            assert binding["test_node"] in source["test_nodes"]
            assert binding["supports"]
            referenced_sources.add(binding["source_id"])

    assert referenced_sources == set(sources)


def test_domain_outcome_coverage_is_honest_about_missing_rejection_goldens() -> None:
    contract = _load_contract()
    cases = {case["case_id"]: case for case in contract["cases"]}
    coverage = {row["domain"]: row for row in contract["outcome_coverage"]}

    assert set(coverage) == _DOMAINS
    for domain, row in coverage.items():
        assert set(row) == {"domain", "completed", "results_rejected"}
        for outcome in ("completed", "results_rejected"):
            entry = row[outcome]
            if entry["available"]:
                assert entry["case_ids"]
                for case_id in entry["case_ids"]:
                    material = cases[case_id]["identity"]["material"]
                    assert material["domain"] == domain
                    assert material["outcome"] == outcome
            else:
                assert entry["case_ids"] == []
                assert entry["reason_code"] == (
                    "no_named_offline_domain_results_rejected_test_at_freeze"
                )
                assert entry["required_terminal_status_if_captured"] == outcome

        assert row["completed"]["available"] is True

    covered_case_ids = {
        case_id
        for row in coverage.values()
        for outcome in ("completed", "results_rejected")
        for case_id in row[outcome]["case_ids"]
    }
    assert covered_case_ids == set(cases)


def test_cases_freeze_semantics_not_environment_sensitive_numbers_or_bytes() -> None:
    contract = _load_contract()
    forbidden_exact_metrics = {
        "answer_f1",
        "latency_ms",
        "mae",
        "mae_cv_mean",
        "mae_cv_std",
        "mae_holdout",
        "mae_lcso",
        "mae_scaffold",
        "r2",
        "r2_cv_mean",
        "r2_holdout",
        "r2_lcso",
        "r2_scaffold",
        "rmse",
        "rmse_holdout",
        "rmse_lcso",
        "rmse_scaffold",
    }

    for case in contract["cases"]:
        metrics = case["metrics"]
        assert metrics["required_keys"]
        assert not (set(metrics["exact_values"]) & forbidden_exact_metrics)
        assert "latency_ms" not in metrics["exact_values"]

        events = case["events"]
        assert events["legacy_identity_class"] == "unkeyed_unhashed"
        assert isinstance(events["required_types"], list)
        assert isinstance(events["required_order"], list)
        assert events["payload_bytes_frozen"] is False

        artifacts = case["artifacts"]
        assert isinstance(artifacts["required_kinds"], list)
        assert isinstance(artifacts["required_relative_paths"], list)
        assert isinstance(artifacts["semantic_invariants"], list)
        assert artifacts["content_bytes_frozen"] is False

        terminal = case["terminal"]
        assert terminal["surface"] in {"plugin_return", "run_ledger"}
        if case["identity"]["material"]["execution_surface"] == "domain_plugin":
            assert terminal["required_status"] == "returned_successfully"
        else:
            assert terminal["required_status"] == case["identity"]["material"]["outcome"]


def test_persisted_driver_event_order_has_a_frozen_historical_witness() -> None:
    """Back required order with payload-free source rows, not only an event vocabulary test."""

    def is_subsequence(sequence: list[str], expected: list[str]) -> bool:
        iterator = iter(sequence)
        return all(any(observed == item for observed in iterator) for item in expected)

    contract = _load_contract()
    source = json.loads(_RUN_SOURCE_PATH.read_text(encoding="utf-8"))
    source_cases = source["cases"]
    for case in contract["cases"]:
        material = case["identity"]["material"]
        if material["execution_surface"] != "legacy_driver_dry_run":
            continue
        required_order = case["events"]["required_order"]
        assert required_order
        witnesses = [
            candidate["event_source"]["event_types"]
            for candidate in source_cases
            if candidate["domain"] == material["domain"]
            and candidate["terminal_status"] == material["outcome"]
        ]
        assert witnesses, case["case_id"]
        assert any(is_subsequence(event_types, required_order) for event_types in witnesses), case[
            "case_id"
        ]
