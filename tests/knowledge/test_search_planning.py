from __future__ import annotations

import pytest
from pydantic import ValidationError

import aletheia.knowledge as k
from .f8s2_fixtures import (
    build_manifests,
    build_protocol,
    build_search_plan,
    build_term_set,
    sha,
)
from .test_schema_spike import _time


def test_query_plan_is_deterministic_complete_and_budgeted() -> None:
    first = build_search_plan()
    second = build_search_plan()

    assert first == second
    assert first.plan_sha256 == second.plan_sha256
    assert tuple(adapter.source_id for adapter in first.adapters) == (
        first.protocol.planned_source_ids
    )
    assert set(first.protocol.required_query_families) <= {
        query.family for query in first.queries
    }
    assert sum(query.max_pages for query in first.queries) <= first.protocol.max_queries
    for direction in (k.QueryFamily.CITATION_BACKWARD, k.QueryFamily.CITATION_FORWARD):
        assert {
            query.seed_paper_snapshot_sha256
            for query in first.queries
            if query.family is direction
        } == set(first.protocol.seed_paper_snapshot_sha256s)


def test_model_terms_can_widen_but_cannot_replace_core_axes() -> None:
    valid = build_term_set()
    supplement = next(
        term for term in valid.terms if term.origin is k.SearchTermOrigin.MODEL_SUPPLEMENT
    )
    assert supplement.family is k.QueryFamily.SYNONYM

    with pytest.raises(ValidationError, match="only widen synonym or adjacent-field"):
        k.SearchTerm(
            family="method",
            value="model invented core method",
            origin="model_supplement",
            generator_manifest_sha256=sha("model"),
        )

    missing_method = tuple(
        term
        for term in valid.terms
        if not (
            term.family is k.QueryFamily.METHOD
            and term.origin is k.SearchTermOrigin.DETERMINISTIC_CORE
        )
    )
    with pytest.raises(ValidationError, match="missing query families: method"):
        k.QueryTermSet(
            term_set_id="missing-method",
            terms=missing_method,
            deterministic_builder_sha256=sha("builder"),
            frozen_at=valid.frozen_at,
        )


def test_plan_refuses_planner_adapter_and_budget_drift() -> None:
    protocol = build_protocol()
    terms = build_term_set()
    manifests = build_manifests()

    drift_payload = protocol.model_dump(mode="python")
    drift_payload["query_planner_sha256"] = sha("different planner")
    drifted_protocol = k.SearchProtocol.model_validate(drift_payload)
    with pytest.raises(ValueError, match="must freeze the F8-S2 query planner"):
        k.build_search_execution_plan(
            plan_id="planner-drift",
            protocol=drifted_protocol,
            term_set=terms,
            adapters=manifests,
            frozen_at=protocol.frozen_at,
        )

    with pytest.raises(ValueError, match="planned-source order"):
        k.build_search_execution_plan(
            plan_id="adapter-order-drift",
            protocol=protocol,
            term_set=terms,
            adapters=tuple(reversed(manifests)),
            frozen_at=protocol.frozen_at,
        )

    tiny_payload = protocol.model_dump(mode="python")
    tiny_payload["max_queries"] = 1
    tiny = k.SearchProtocol.model_validate(tiny_payload)
    with pytest.raises(ValidationError, match="page requests exceed"):
        k.build_search_execution_plan(
            plan_id="budget-drift",
            protocol=tiny,
            term_set=terms,
            adapters=manifests,
            frozen_at=protocol.frozen_at,
        )


def test_metadata_adapter_contract_refuses_text_and_secret_filters() -> None:
    base = build_manifests()[0]
    payload = base.model_dump(mode="python")
    payload["included_fields"] = (*base.included_fields, "abstract")
    with pytest.raises(ValidationError, match="cannot request abstracts"):
        k.ProviderAdapterManifest.model_validate(payload)

    payload = base.model_dump(mode="python")
    payload["excluded_fields"] = ("abstract",)
    with pytest.raises(ValidationError, match="explicitly exclude all text fields"):
        k.ProviderAdapterManifest.model_validate(payload)

    with pytest.raises(ValidationError, match="credentials"):
        k.RequestFilter(name="api_key", value="must-not-freeze")


def test_only_citation_capable_sources_receive_seed_queries() -> None:
    protocol = build_protocol()
    only_source = (protocol.planned_source_ids[0],)
    plan = build_search_plan(citation_sources=only_source)
    citation_queries = [
        query
        for query in plan.queries
        if query.family
        in {k.QueryFamily.CITATION_BACKWARD, k.QueryFamily.CITATION_FORWARD}
    ]
    assert citation_queries
    assert {query.source_id for query in citation_queries} == set(only_source)


def test_term_and_filter_canonicalization_is_hash_stable() -> None:
    terms = k.build_query_term_set(
        term_set_id="canonical-terms",
        deterministic_terms={
            family: (f"  canonical   {family.value}  ",)
            for family in (
                k.QueryFamily.QUEST,
                k.QueryFamily.MECHANISM,
                k.QueryFamily.OBJECT,
                k.QueryFamily.METHOD,
                k.QueryFamily.DATASET,
                k.QueryFamily.RESULT,
                k.QueryFamily.SYNONYM,
                k.QueryFamily.ADJACENT_FIELD,
                k.QueryFamily.NEGATION,
            )
        },
        deterministic_builder_sha256=sha("canonical-builder"),
        frozen_at=_time("2024-12-29T00:00:00Z"),
    )
    assert all("  " not in term.value for term in terms.terms)
    assert terms == k.QueryTermSet.model_validate_json(terms.model_dump_json())

    with pytest.raises(ValidationError, match="canonical whitespace"):
        k.RequestFilter(name="year", value="2020  2024")
