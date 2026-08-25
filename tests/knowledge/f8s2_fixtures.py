from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

import aletheia.knowledge as k
from .test_schema_spike import _build_bundle, _time


def sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def build_term_set() -> k.QueryTermSet:
    return k.build_query_term_set(
        term_set_id="f8s2-synthetic-terms-v1",
        deterministic_terms={
            k.QueryFamily.QUEST: ("adaptive calibration",),
            k.QueryFamily.MECHANISM: ("distribution shift correction",),
            k.QueryFamily.OBJECT: ("sensor stream",),
            k.QueryFamily.METHOD: ("online calibration method",),
            k.QueryFamily.DATASET: ("synthetic drift fixture",),
            k.QueryFamily.RESULT: ("calibration accuracy",),
            k.QueryFamily.SYNONYM: ("probability recalibration",),
            k.QueryFamily.ADJACENT_FIELD: ("concept drift adaptation",),
            k.QueryFamily.NEGATION: ("without online calibration",),
            k.QueryFamily.AUTHOR: ("Fixture Author",),
        },
        model_supplements=(
            (k.QueryFamily.SYNONYM, "confidence realignment", sha("model-v1")),
        ),
        deterministic_builder_sha256=sha("f8s2-deterministic-term-builder-v1"),
        frozen_at=_time("2024-12-29T00:00:00Z"),
    )


def build_protocol(*, max_queries: int = 1_000) -> k.SearchProtocol:
    base = _build_bundle()["bundle"].search_protocol
    payload = base.model_dump(mode="python")
    payload.update(
        {
            "protocol_id": "f8s2-synthetic-search-protocol-v1",
            "max_queries": max_queries,
            "query_planner_sha256": k.QUERY_PLANNER_IDENTITY_SHA256,
            "frozen_at": _time("2024-12-29T00:00:00Z"),
        }
    )
    return k.SearchProtocol.model_validate(payload)


def build_manifests(
    *,
    max_pages: int = 1,
    minimum_request_interval_seconds: float = 0.0,
    citation_sources: tuple[str, ...] | None = None,
) -> tuple[k.ProviderAdapterManifest, ...]:
    protocol = build_protocol()
    citation_sources = citation_sources or protocol.planned_source_ids
    manifests: list[k.ProviderAdapterManifest] = []
    for source_id in protocol.planned_source_ids:
        supported = tuple(
            family
            for family in k.QueryFamily
            if family not in {
                k.QueryFamily.CITATION_BACKWARD,
                k.QueryFamily.CITATION_FORWARD,
            }
            or source_id in citation_sources
        )
        manifests.append(
            k.ProviderAdapterManifest(
                source_id=source_id,
                adapter_name=f"Synthetic {source_id} metadata adapter",
                adapter_version="1.0.0",
                adapter_sha256=sha(f"adapter:{source_id}:v1"),
                parser_sha256=sha(f"parser:{source_id}:v1"),
                response_schema_sha256=sha(f"response-schema:{source_id}:v1"),
                terms_sha256=sha(f"terms:{source_id}:2024-12-29"),
                media_types=("application/json",),
                included_fields=("id", "title", "authors", "year", "doi", "references"),
                supports_query_families=supported,
                pagination_kind=("none" if max_pages == 1 else "cursor"),
                page_size=10,
                max_pages=max_pages,
                max_response_bytes=1024 * 1024,
                minimum_request_interval_seconds=minimum_request_interval_seconds,
                frozen_at=_time("2024-12-29T00:00:00Z"),
            )
        )
    return tuple(manifests)


def build_search_plan(
    *,
    max_pages: int = 1,
    minimum_request_interval_seconds: float = 0.0,
    citation_sources: tuple[str, ...] | None = None,
    citation_traversal_policy_sha256: str | None = None,
) -> k.SearchExecutionPlan:
    protocol = build_protocol(max_queries=2_000)
    manifests = build_manifests(
        max_pages=max_pages,
        minimum_request_interval_seconds=minimum_request_interval_seconds,
        citation_sources=citation_sources,
    )
    return k.build_search_execution_plan(
        plan_id=f"f8s2-synthetic-plan-{max_pages}",
        protocol=protocol,
        term_set=build_term_set(),
        adapters=manifests,
        frozen_at=_time("2024-12-29T00:00:00Z"),
        citation_traversal_policy_sha256=citation_traversal_policy_sha256,
    )


def build_citation_policy(
    *,
    maximum_rounds: int = 4,
    consecutive_saturated_rounds: int = 1,
    maximum_requests: int = 2_000,
    maximum_expanded_papers: int = 1_000,
) -> k.CitationTraversalPolicy:
    return k.CitationTraversalPolicy(
        policy_id="f8s2-synthetic-citation-policy-v1",
        saturation_rule=k.SaturationRule(
            minimum_rounds=2,
            maximum_rounds=maximum_rounds,
            marginal_new_relevant_fraction=0.05,
            consecutive_saturated_rounds=consecutive_saturated_rounds,
        ),
        maximum_requests=maximum_requests,
        maximum_expanded_papers=maximum_expanded_papers,
        frozen_at=_time("2024-12-29T00:00:00Z"),
    )


class StepClock:
    def __init__(self) -> None:
        self.current = _time("2024-12-30T00:00:00Z")

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(milliseconds=100)
        return value


class SyntheticSearchAdapter:
    def __init__(self, manifest: k.ProviderAdapterManifest) -> None:
        self._manifest = manifest
        self.fetch_calls: list[tuple[str, str | None]] = []
        self.page_counts: dict[str, int] = {}
        self.repeat_across_pages: set[str] = set()
        self.never_terminal: set[str] = set()
        self.fetch_errors: dict[str, Exception] = {}
        self.parse_errors: dict[str, Exception] = {}
        self.status_codes: dict[str, int] = {}
        self.forbidden_text_queries: set[str] = set()
        self.citation_graph: dict[tuple[str, k.QueryFamily], tuple[str, ...]] | None = None
        self.fail_citation_rounds: set[int] = set()

    @property
    def manifest(self) -> k.ProviderAdapterManifest:
        return self._manifest

    async def fetch(
        self, query: k.PlannedSearchQuery, page_token: str | None
    ) -> k.RawProviderResponse:
        self.fetch_calls.append((query.logical_query_id, page_token))
        if (
            query.family
            in {k.QueryFamily.CITATION_BACKWARD, k.QueryFamily.CITATION_FORWARD}
            and query.round_index in self.fail_citation_rounds
        ):
            raise k.CircuitOpenError("synthetic derived-round circuit open")
        error = self.fetch_errors.get(query.logical_query_id)
        if error is not None:
            raise error
        page_index = int(page_token.split(":", 1)[1]) if page_token else 0
        count = self.page_counts.get(query.logical_query_id, 1)
        terminal = page_index + 1 >= count and query.logical_query_id not in self.never_terminal
        next_page = None if terminal else f"page:{page_index + 1}"
        hit_page = 0 if query.logical_query_id in self.repeat_across_pages else page_index
        if query.family in {
            k.QueryFamily.CITATION_BACKWARD,
            k.QueryFamily.CITATION_FORWARD,
        } and self.citation_graph is not None:
            assert query.seed_paper_snapshot_sha256 is not None
            paper_hashes = self.citation_graph.get(
                (query.seed_paper_snapshot_sha256, query.family), ()
            )
        else:
            paper_hashes = (sha(f"paper:{query.logical_query_id}:{hit_page}"),)
        payload: dict[str, object] = {
            "source_id": self.manifest.source_id,
            "query_id": query.logical_query_id,
            "page": page_index,
            "next_cursor": next_page,
            "items": [
                {
                    "id": f"record:{query.logical_query_id}:{hit_page}:{index}",
                    "paper_snapshot_sha256": paper_sha256,
                    "title": f"Synthetic metadata result {hit_page}:{index}",
                    "authors": ["Fixture Author"],
                    "year": 2024,
                    "score": 1.0 - page_index / 100,
                }
                for index, paper_sha256 in enumerate(paper_hashes)
            ],
        }
        if query.logical_query_id in self.forbidden_text_queries:
            payload["abstract"] = "text that must never be archived"
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return k.RawProviderResponse(
            body=body,
            media_type="application/json",
            status_code=self.status_codes.get(query.logical_query_id, 200),
            response_headers_sha256=sha(
                f"headers:{self.manifest.source_id}:{query.logical_query_id}:{page_index}"
            ),
        )

    def parse(
        self, query: k.PlannedSearchQuery, body: bytes
    ) -> k.ParsedProviderPage:
        error = self.parse_errors.get(query.logical_query_id)
        if error is not None:
            raise error
        payload = json.loads(body)
        hits = tuple(
            k.SearchHit(
                rank=index,
                paper_snapshot_sha256=item["paper_snapshot_sha256"],
                provider_record_id=item["id"],
                retrieval_score=item["score"],
            )
            for index, item in enumerate(payload["items"], start=1)
        )
        return k.ParsedProviderPage(
            hits=hits,
            next_page_token=payload["next_cursor"],
            terminal=payload["next_cursor"] is None,
        )


def build_adapters(
    plan: k.SearchExecutionPlan,
) -> dict[str, SyntheticSearchAdapter]:
    return {
        manifest.source_id: SyntheticSearchAdapter(manifest)
        for manifest in plan.adapters
    }
