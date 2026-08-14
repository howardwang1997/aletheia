"""Deterministic, fail-closed search planning for the F8 knowledge boundary.

This module deliberately does not perform network I/O.  It freezes the exact query axes,
provider/parser identities, metadata-only response policy, pagination budgets, and citation
seeds before execution.  Model-generated terms may widen synonym or adjacent-field searches,
but cannot replace any deterministic core query family.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aletheia.knowledge.schemas import KnowledgeModel, QueryFamily, SearchProtocol
from aletheia.reproducibility.manifest import content_sha256


_CORE_FAMILIES = (
    QueryFamily.QUEST,
    QueryFamily.MECHANISM,
    QueryFamily.OBJECT,
    QueryFamily.METHOD,
    QueryFamily.DATASET,
    QueryFamily.RESULT,
    QueryFamily.SYNONYM,
    QueryFamily.ADJACENT_FIELD,
    QueryFamily.NEGATION,
)
_CITATION_FAMILIES = (QueryFamily.CITATION_BACKWARD, QueryFamily.CITATION_FORWARD)
_MODEL_SUPPLEMENT_FAMILIES = {QueryFamily.SYNONYM, QueryFamily.ADJACENT_FIELD}
_TEXT_FIELDS = {"abstract", "body", "full_text", "fulltext", "source_text", "summary"}
_SECRET_PARAMETER_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)


QUERY_PLANNER_IDENTITY_SHA256 = content_sha256(
    {
        "component": "aletheia.knowledge.search.build_search_execution_plan",
        "schema_version": 1,
        "ordering": "protocol-source-order_then_query-family-order_then_seed-order",
        "model_supplements": "synonym_or_adjacent_only_and_never_core_replacement",
        "citation_policy": "both_directions_for_every_seed_on_every_capable_source",
        "failure_policy": "record_and_fail_hard_coverage",
    }
)


class SearchTermOrigin(str, Enum):
    DETERMINISTIC_CORE = "deterministic_core"
    MODEL_SUPPLEMENT = "model_supplement"


class PaginationKind(str, Enum):
    NONE = "none"
    OFFSET = "offset"
    CURSOR = "cursor"


class SearchTerm(KnowledgeModel):
    schema_version: Literal[1] = 1
    family: QueryFamily
    value: str = Field(min_length=1, max_length=512)
    origin: SearchTermOrigin
    generator_manifest_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _term_is_canonical_and_authorized(self) -> "SearchTerm":
        canonical = " ".join(self.value.split())
        if self.value != canonical:
            raise ValueError("search terms must use canonical whitespace")
        if any(ord(character) < 32 for character in self.value):
            raise ValueError("search terms cannot contain control characters")
        if self.family in _CITATION_FAMILIES:
            raise ValueError("citation queries are derived from frozen seed identities")
        if self.origin is SearchTermOrigin.MODEL_SUPPLEMENT:
            if self.family not in _MODEL_SUPPLEMENT_FAMILIES:
                raise ValueError(
                    "model supplements may only widen synonym or adjacent-field queries"
                )
            if self.generator_manifest_sha256 is None:
                raise ValueError("model supplements require an exact generator manifest")
        elif self.generator_manifest_sha256 is not None:
            raise ValueError("deterministic terms cannot claim a model generator")
        return self

    @property
    def term_sha256(self) -> str:
        return content_sha256(self)


def _term_sort_key(term: SearchTerm) -> tuple[int, str, str, str]:
    family_index = list(QueryFamily).index(term.family)
    return (
        family_index,
        term.value.casefold(),
        term.origin.value,
        term.generator_manifest_sha256 or "",
    )


class QueryTermSet(KnowledgeModel):
    schema_version: Literal[1] = 1
    term_set_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    terms: tuple[SearchTerm, ...] = Field(min_length=9)
    deterministic_builder_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _core_axes_cannot_be_replaced(self) -> "QueryTermSet":
        identities = [(term.family, term.value.casefold()) for term in self.terms]
        if len(identities) != len(set(identities)):
            raise ValueError("search terms must be unique within each family")
        if self.terms != tuple(sorted(self.terms, key=_term_sort_key)):
            raise ValueError("search terms must use canonical family/value ordering")
        deterministic_families = {
            term.family
            for term in self.terms
            if term.origin is SearchTermOrigin.DETERMINISTIC_CORE
        }
        missing = set(_CORE_FAMILIES) - deterministic_families
        if missing:
            names = ", ".join(sorted(family.value for family in missing))
            raise ValueError(f"deterministic core terms are missing query families: {names}")
        return self

    def terms_for(self, family: QueryFamily) -> tuple[SearchTerm, ...]:
        return tuple(term for term in self.terms if term.family is family)

    @property
    def term_set_sha256(self) -> str:
        return content_sha256(self)


def build_query_term_set(
    *,
    term_set_id: str,
    deterministic_terms: Mapping[QueryFamily | str, Sequence[str]],
    deterministic_builder_sha256: str,
    frozen_at: AwareDatetime,
    model_supplements: Sequence[tuple[QueryFamily | str, str, str]] = (),
) -> QueryTermSet:
    """Canonicalize a term set while keeping model additions visibly supplemental."""

    terms: list[SearchTerm] = []
    for raw_family, values in deterministic_terms.items():
        family = QueryFamily(raw_family)
        for value in values:
            terms.append(
                SearchTerm(
                    family=family,
                    value=" ".join(value.split()),
                    origin=SearchTermOrigin.DETERMINISTIC_CORE,
                )
            )
    for raw_family, value, generator_sha256 in model_supplements:
        terms.append(
            SearchTerm(
                family=QueryFamily(raw_family),
                value=" ".join(value.split()),
                origin=SearchTermOrigin.MODEL_SUPPLEMENT,
                generator_manifest_sha256=generator_sha256,
            )
        )
    return QueryTermSet(
        term_set_id=term_set_id,
        terms=tuple(sorted(terms, key=_term_sort_key)),
        deterministic_builder_sha256=deterministic_builder_sha256,
        frozen_at=frozen_at,
    )


class RequestFilter(KnowledgeModel):
    schema_version: Literal[1] = 1
    name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,127}$")
    value: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def _filter_is_safe_to_freeze(self) -> "RequestFilter":
        lowered = self.name.casefold()
        if any(fragment in lowered for fragment in _SECRET_PARAMETER_FRAGMENTS):
            raise ValueError("credentials and opaque access tokens cannot enter a search plan")
        if self.value != " ".join(self.value.split()):
            raise ValueError("request filters must use canonical whitespace")
        return self


class ProviderAdapterManifest(KnowledgeModel):
    schema_version: Literal[1] = 1
    source_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,79}$")
    adapter_name: str = Field(min_length=1, max_length=256)
    adapter_version: str = Field(min_length=1, max_length=128)
    adapter_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terms_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_types: tuple[str, ...] = Field(min_length=1)
    included_fields: tuple[str, ...] = Field(min_length=1)
    excluded_fields: tuple[str, ...] = (
        "abstract",
        "body",
        "full_text",
        "fulltext",
        "source_text",
        "summary",
    )
    content_access_class: Literal["metadata_only"] = "metadata_only"
    supports_query_families: tuple[QueryFamily, ...] = Field(min_length=1)
    pagination_kind: PaginationKind
    page_size: int = Field(ge=1, le=10_000)
    max_pages: int = Field(ge=1, le=10_000)
    max_response_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    minimum_request_interval_seconds: float = Field(ge=0.0, le=3_600.0)
    automated_retrieval_permitted: Literal[True] = True
    failure_semantics: Literal["record_and_fail_hard_coverage"] = (
        "record_and_fail_hard_coverage"
    )
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _manifest_is_metadata_only_and_canonical(self) -> "ProviderAdapterManifest":
        if len(self.media_types) != len(set(self.media_types)):
            raise ValueError("adapter media types must be unique")
        if len(self.included_fields) != len(set(self.included_fields)):
            raise ValueError("adapter included fields must be unique")
        included = {field.casefold() for field in self.included_fields}
        excluded = {field.casefold() for field in self.excluded_fields}
        if _TEXT_FIELDS - excluded:
            raise ValueError("metadata-only adapters must explicitly exclude all text fields")
        if included & _TEXT_FIELDS:
            raise ValueError("metadata-only adapters cannot request abstracts or full text")
        families = list(self.supports_query_families)
        if len(families) != len(set(families)):
            raise ValueError("adapter query capabilities must be unique")
        if families != sorted(families, key=lambda item: list(QueryFamily).index(item)):
            raise ValueError("adapter query capabilities must use canonical family ordering")
        if self.pagination_kind is PaginationKind.NONE and self.max_pages != 1:
            raise ValueError("non-paginated adapters must have exactly one page")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class PlannedSearchQuery(KnowledgeModel):
    schema_version: Literal[1] = 1
    logical_query_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,175}$")
    source_id: str = Field(min_length=1, max_length=80)
    adapter_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    family: QueryFamily
    query_text: str = Field(min_length=1, max_length=8192)
    filters: tuple[RequestFilter, ...]
    round_index: int = Field(ge=0, le=100)
    seed_paper_snapshot_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    generated_from_term_sha256s: tuple[str, ...] = ()
    max_results: int = Field(ge=1, le=10_000)
    max_pages: int = Field(ge=1, le=10_000)

    @model_validator(mode="after")
    def _query_is_exact_and_canonical(self) -> "PlannedSearchQuery":
        if self.query_text != " ".join(self.query_text.split()):
            raise ValueError("planned query text must use canonical whitespace")
        filter_keys = [(item.name, item.value) for item in self.filters]
        if filter_keys != sorted(filter_keys) or len(filter_keys) != len(set(filter_keys)):
            raise ValueError("request filters must be unique and canonically ordered")
        is_citation = self.family in _CITATION_FAMILIES
        if is_citation != (self.seed_paper_snapshot_sha256 is not None):
            raise ValueError("citation queries require one seed, and non-citation queries forbid it")
        if is_citation and self.generated_from_term_sha256s:
            raise ValueError("citation queries derive from seed identities, not free-text terms")
        if not is_citation and not self.generated_from_term_sha256s:
            raise ValueError("free-text queries require their exact source-term identities")
        if len(self.generated_from_term_sha256s) != len(
            set(self.generated_from_term_sha256s)
        ):
            raise ValueError("planned query term identities must be unique")
        return self

    @property
    def filters_sha256(self) -> str:
        return content_sha256(
            [item.model_dump(mode="json") for item in self.filters]
        )

    @property
    def logical_query_sha256(self) -> str:
        return content_sha256(self)


class SearchExecutionPlan(KnowledgeModel):
    schema_version: Literal[1] = 1
    plan_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    plan_kind: Literal["initial_search", "citation_round"] = "initial_search"
    parent_execution_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    citation_frontier_sha256s: tuple[str, ...] = ()
    citation_traversal_policy_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    derivation_policy_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    protocol: SearchProtocol
    term_set: QueryTermSet
    adapters: tuple[ProviderAdapterManifest, ...] = Field(min_length=2)
    queries: tuple[PlannedSearchQuery, ...] = Field(min_length=1)
    frozen_at: AwareDatetime
    state: Literal["frozen"] = "frozen"

    @model_validator(mode="after")
    def _plan_closes_the_protocol(self) -> "SearchExecutionPlan":
        if self.protocol.query_planner_sha256 != QUERY_PLANNER_IDENTITY_SHA256:
            raise ValueError("search protocol is bound to a different query planner")
        if self.frozen_at < self.protocol.frozen_at or self.frozen_at < self.term_set.frozen_at:
            raise ValueError("search plan cannot predate its frozen protocol or terms")
        source_ids = tuple(adapter.source_id for adapter in self.adapters)
        if source_ids != self.protocol.planned_source_ids:
            raise ValueError("adapter order and membership must exactly match planned sources")
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("search plan adapter sources must be unique")
        manifests = {adapter.source_id: adapter for adapter in self.adapters}
        query_ids = [query.logical_query_id for query in self.queries]
        query_hashes = [query.logical_query_sha256 for query in self.queries]
        if len(query_ids) != len(set(query_ids)) or len(query_hashes) != len(set(query_hashes)):
            raise ValueError("logical search queries must have unique IDs and contents")
        for query in self.queries:
            adapter = manifests.get(query.source_id)
            if adapter is None or query.adapter_manifest_sha256 != adapter.manifest_sha256:
                raise ValueError("planned query is not bound to its exact adapter manifest")
            if query.family not in adapter.supports_query_families:
                raise ValueError("planned query exceeds adapter capabilities")
            if query.max_pages > adapter.max_pages:
                raise ValueError("planned query exceeds the adapter page budget")
            if query.max_results > min(
                adapter.page_size, self.protocol.max_results_per_query
            ):
                raise ValueError("planned query exceeds result limits")
        if sum(query.max_pages for query in self.queries) > self.protocol.max_queries:
            raise ValueError("planned page requests exceed the frozen query budget")
        used_sources = {query.source_id for query in self.queries}
        if self.plan_kind == "initial_search":
            if (
                self.parent_execution_sha256 is not None
                or self.citation_frontier_sha256s
                or self.derivation_policy_sha256 is not None
            ):
                raise ValueError("initial search plans cannot claim a derived citation frontier")
            covered = {query.family for query in self.queries}
            missing = set(self.protocol.required_query_families) - covered
            if missing:
                names = ", ".join(sorted(family.value for family in missing))
                raise ValueError(f"search plan misses required query families: {names}")
            if used_sources != set(source_ids):
                raise ValueError("every planned source must receive at least one query")
            expected_seeds = set(self.protocol.seed_paper_snapshot_sha256s)
        else:
            if (
                self.parent_execution_sha256 is None
                or self.derivation_policy_sha256 is None
                or not self.citation_frontier_sha256s
            ):
                raise ValueError(
                    "citation-round plans require parent, derivation policy, and frontier"
                )
            if self.citation_traversal_policy_sha256 != self.derivation_policy_sha256:
                raise ValueError("citation round is bound to a different traversal policy")
            if len(self.citation_frontier_sha256s) != len(
                set(self.citation_frontier_sha256s)
            ) or self.citation_frontier_sha256s != tuple(
                sorted(self.citation_frontier_sha256s)
            ):
                raise ValueError("citation frontier identities must be unique and sorted")
            if any(
                len(identity) != 64
                or any(character not in "0123456789abcdef" for character in identity)
                for identity in self.citation_frontier_sha256s
            ):
                raise ValueError("citation frontier identities must use SHA-256")
            if any(query.family not in _CITATION_FAMILIES for query in self.queries):
                raise ValueError("citation-round plans may contain only citation queries")
            capable_sources = {
                adapter.source_id
                for adapter in self.adapters
                if set(adapter.supports_query_families) & set(_CITATION_FAMILIES)
            }
            if used_sources != capable_sources:
                raise ValueError("citation rounds must query every citation-capable source")
            if any(query.round_index < 1 for query in self.queries):
                raise ValueError("derived citation rounds must have a positive round index")
            expected_seeds = set(self.citation_frontier_sha256s)
        for family in _CITATION_FAMILIES:
            observed_seeds = {
                query.seed_paper_snapshot_sha256
                for query in self.queries
                if query.family is family
            }
            if observed_seeds != expected_seeds:
                raise ValueError(
                    f"{family.value} queries must exactly cover every frozen seed paper"
                )
        return self

    @property
    def plan_sha256(self) -> str:
        return content_sha256(self)


def _quote_term(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _logical_query_id(
    *,
    index: int,
    source_id: str,
    family: QueryFamily,
    identity_payload: object,
) -> str:
    suffix = content_sha256(identity_payload)[:12]
    return f"q{index:05d}:{source_id}:{family.value}:{suffix}"


def build_search_execution_plan(
    *,
    plan_id: str,
    protocol: SearchProtocol,
    term_set: QueryTermSet,
    adapters: Sequence[ProviderAdapterManifest],
    frozen_at: AwareDatetime,
    citation_traversal_policy_sha256: str | None = None,
) -> SearchExecutionPlan:
    """Build the only supported F8-S2 plan ordering from frozen inputs."""

    if protocol.query_planner_sha256 != QUERY_PLANNER_IDENTITY_SHA256:
        raise ValueError("search protocol must freeze the F8-S2 query planner identity")
    adapter_tuple = tuple(adapters)
    adapter_by_source = {adapter.source_id: adapter for adapter in adapter_tuple}
    if tuple(adapter_by_source) != protocol.planned_source_ids:
        raise ValueError("adapters must be supplied once in frozen planned-source order")

    queries: list[PlannedSearchQuery] = []
    family_order = [
        family for family in QueryFamily if family in set(protocol.required_query_families)
    ]
    common_filters = tuple(
        sorted(
            (
                RequestFilter(name="cutoff_time", value=protocol.cutoff_time.isoformat()),
                RequestFilter(
                    name="corpus_snapshot_sha256", value=protocol.corpus_snapshot_sha256
                ),
            ),
            key=lambda item: (item.name, item.value),
        )
    )
    for source_id in protocol.planned_source_ids:
        adapter = adapter_by_source[source_id]
        for family in family_order:
            if family not in adapter.supports_query_families:
                continue
            terms = term_set.terms_for(family)
            if not terms:
                raise ValueError(f"term set has no terms for required family {family.value}")
            query_text = " OR ".join(_quote_term(term.value) for term in terms)
            identity = {
                "source_id": source_id,
                "family": family.value,
                "term_sha256s": [term.term_sha256 for term in terms],
                "filters": [item.model_dump(mode="json") for item in common_filters],
            }
            query_id = _logical_query_id(
                index=len(queries),
                source_id=source_id,
                family=family,
                identity_payload=identity,
            )
            queries.append(
                PlannedSearchQuery(
                    logical_query_id=query_id,
                    source_id=source_id,
                    adapter_manifest_sha256=adapter.manifest_sha256,
                    family=family,
                    query_text=query_text,
                    filters=common_filters,
                    round_index=0,
                    generated_from_term_sha256s=tuple(
                        term.term_sha256 for term in terms
                    ),
                    max_results=min(
                        adapter.page_size, protocol.max_results_per_query
                    ),
                    max_pages=adapter.max_pages,
                )
            )

    for source_id in protocol.planned_source_ids:
        adapter = adapter_by_source[source_id]
        for family in _CITATION_FAMILIES:
            if family not in adapter.supports_query_families:
                continue
            for seed in protocol.seed_paper_snapshot_sha256s:
                identity = {
                    "source_id": source_id,
                    "family": family.value,
                    "seed_paper_snapshot_sha256": seed,
                    "filters": [item.model_dump(mode="json") for item in common_filters],
                }
                query_id = _logical_query_id(
                    index=len(queries),
                    source_id=source_id,
                    family=family,
                    identity_payload=identity,
                )
                queries.append(
                    PlannedSearchQuery(
                        logical_query_id=query_id,
                        source_id=source_id,
                        adapter_manifest_sha256=adapter.manifest_sha256,
                        family=family,
                        query_text=f"seed:{seed}",
                        filters=common_filters,
                        round_index=0,
                        seed_paper_snapshot_sha256=seed,
                        max_results=min(
                            adapter.page_size, protocol.max_results_per_query
                        ),
                        max_pages=adapter.max_pages,
                    )
                )

    return SearchExecutionPlan(
        plan_id=plan_id,
        protocol=protocol,
        term_set=term_set,
        adapters=adapter_tuple,
        queries=tuple(queries),
        citation_traversal_policy_sha256=citation_traversal_policy_sha256,
        frozen_at=frozen_at,
    )


def build_citation_round_execution_plan(
    *,
    plan_id: str,
    protocol: SearchProtocol,
    term_set: QueryTermSet,
    adapters: Sequence[ProviderAdapterManifest],
    frontier_paper_snapshot_sha256s: Sequence[str],
    parent_execution_sha256: str,
    derivation_policy_sha256: str,
    round_index: int,
    frozen_at: AwareDatetime,
) -> SearchExecutionPlan:
    """Mechanically derive one complete two-direction citation round.

    The caller supplies the entire sorted frontier discovered by the preceding committed round;
    there is no model-controlled seed selection in this derivation step.
    """

    if round_index < 1:
        raise ValueError("derived citation rounds begin at round index one")
    frontier = tuple(sorted(frontier_paper_snapshot_sha256s))
    if not frontier or len(frontier) != len(set(frontier)):
        raise ValueError("citation frontier must be a non-empty set of paper identities")
    adapter_tuple = tuple(adapters)
    adapter_by_source = {adapter.source_id: adapter for adapter in adapter_tuple}
    if tuple(adapter_by_source) != protocol.planned_source_ids:
        raise ValueError("adapters must be supplied once in frozen planned-source order")
    common_filters = tuple(
        sorted(
            (
                RequestFilter(name="cutoff_time", value=protocol.cutoff_time.isoformat()),
                RequestFilter(
                    name="corpus_snapshot_sha256", value=protocol.corpus_snapshot_sha256
                ),
            ),
            key=lambda item: (item.name, item.value),
        )
    )
    queries: list[PlannedSearchQuery] = []
    for source_id in protocol.planned_source_ids:
        adapter = adapter_by_source[source_id]
        for family in _CITATION_FAMILIES:
            if family not in adapter.supports_query_families:
                continue
            for seed in frontier:
                identity = {
                    "parent_execution_sha256": parent_execution_sha256,
                    "derivation_policy_sha256": derivation_policy_sha256,
                    "round_index": round_index,
                    "source_id": source_id,
                    "family": family.value,
                    "seed_paper_snapshot_sha256": seed,
                    "filters": [item.model_dump(mode="json") for item in common_filters],
                }
                query_id = _logical_query_id(
                    index=len(queries),
                    source_id=source_id,
                    family=family,
                    identity_payload=identity,
                )
                queries.append(
                    PlannedSearchQuery(
                        logical_query_id=query_id,
                        source_id=source_id,
                        adapter_manifest_sha256=adapter.manifest_sha256,
                        family=family,
                        query_text=f"seed:{seed}",
                        filters=common_filters,
                        round_index=round_index,
                        seed_paper_snapshot_sha256=seed,
                        max_results=min(
                            adapter.page_size, protocol.max_results_per_query
                        ),
                        max_pages=adapter.max_pages,
                    )
                )
    return SearchExecutionPlan(
        plan_id=plan_id,
        plan_kind="citation_round",
        parent_execution_sha256=parent_execution_sha256,
        citation_frontier_sha256s=frontier,
        citation_traversal_policy_sha256=derivation_policy_sha256,
        derivation_policy_sha256=derivation_policy_sha256,
        protocol=protocol,
        term_set=term_set,
        adapters=adapter_tuple,
        queries=tuple(queries),
        frozen_at=frozen_at,
    )


__all__ = [
    "PaginationKind",
    "PlannedSearchQuery",
    "ProviderAdapterManifest",
    "QUERY_PLANNER_IDENTITY_SHA256",
    "QueryTermSet",
    "RequestFilter",
    "SearchExecutionPlan",
    "SearchTerm",
    "SearchTermOrigin",
    "build_query_term_set",
    "build_citation_round_execution_plan",
    "build_search_execution_plan",
]
