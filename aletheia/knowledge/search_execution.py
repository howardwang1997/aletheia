"""Fail-closed execution and replay audit for frozen F8-S2 search plans."""

from __future__ import annotations

import asyncio
import hashlib
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, model_validator

from aletheia.knowledge.response_archive import (
    ArchivedProviderResponse,
    ArchivedSearchLedger,
    ContentAddressedResponseArchive,
    ResponseArchiveCorruption,
    ResponseArchiveError,
    ResponsePolicyViolation,
)
from aletheia.knowledge.schemas import (
    KnowledgeModel,
    QueryOutcome,
    SearchHit,
    SearchQueryRecord,
    SearchSession,
    SearchStoppingReason,
)
from aletheia.knowledge.search import (
    PaginationKind,
    PlannedSearchQuery,
    ProviderAdapterManifest,
    SearchExecutionPlan,
)
from aletheia.reproducibility.manifest import canonical_json_bytes, content_sha256


class SearchFailureStage(str, Enum):
    PACING = "pacing"
    CIRCUIT_BREAKER = "circuit_breaker"
    TRANSPORT = "transport"
    PROVIDER = "provider"
    ARCHIVE = "archive"
    PARSE = "parse"
    PAGINATION = "pagination"


class SearchFailureKind(str, Enum):
    PACING_FAILURE = "pacing_failure"
    CIRCUIT_OPEN = "circuit_open"
    TRANSPORT_ERROR = "transport_error"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"
    RESPONSE_TOO_LARGE = "response_too_large"
    POLICY_VIOLATION = "policy_violation"
    ARCHIVE_ERROR = "archive_error"
    PARSE_ERROR = "parse_error"
    DUPLICATE_PAGE_HIT = "duplicate_page_hit"
    PAGINATION_INCOMPLETE = "pagination_incomplete"
    ADAPTER_DRIFT = "adapter_drift"
    UNEXPECTED_ERROR = "unexpected_error"


class ReplayItemStatus(str, Enum):
    VERIFIED = "verified"
    UNAVAILABLE = "unavailable"
    MISMATCH = "mismatch"


class ReplayAuditStatus(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    MISMATCH = "mismatch"


class ProviderFetchError(RuntimeError):
    """A provider adapter's intentionally classified request failure."""

    def __init__(
        self,
        message: str,
        *,
        kind: SearchFailureKind = SearchFailureKind.TRANSPORT_ERROR,
        stage: SearchFailureStage = SearchFailureStage.TRANSPORT,
        retryable: bool = True,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.stage = stage
        self.retryable = retryable
        self.status_code = status_code


class CircuitOpenError(ProviderFetchError):
    def __init__(self, message: str = "provider circuit is open") -> None:
        super().__init__(
            message,
            kind=SearchFailureKind.CIRCUIT_OPEN,
            stage=SearchFailureStage.CIRCUIT_BREAKER,
            retryable=True,
        )


class RawProviderResponse(KnowledgeModel):
    """Exact structured provider bytes before the deterministic parser runs."""

    schema_version: Literal[1] = 1
    body: bytes = Field(min_length=1)
    media_type: str = Field(min_length=1, max_length=256)
    status_code: int = Field(ge=100, le=599)
    response_headers_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ParsedProviderPage(KnowledgeModel):
    """License-safe search identities parsed from one archived response page."""

    schema_version: Literal[1] = 1
    hits: tuple[SearchHit, ...]
    next_page_token: str | None = Field(default=None, max_length=8192)
    terminal: bool
    provider_total_results: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _page_is_ordered_and_closed(self) -> "ParsedProviderPage":
        if self.terminal == (self.next_page_token is not None):
            raise ValueError("terminal pages forbid a next token; non-terminal pages require one")
        if self.next_page_token is not None and not self.next_page_token.strip():
            raise ValueError("provider next-page token cannot be blank")
        ranks = [hit.rank for hit in self.hits]
        if ranks and ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("parsed provider hit ranks must be contiguous and ordered")
        hashes = [hit.paper_snapshot_sha256 for hit in self.hits]
        if len(hashes) != len(set(hashes)):
            raise ValueError("one provider page cannot repeat a paper identity")
        return self

    @property
    def page_sha256(self) -> str:
        return content_sha256(self)


class SearchProviderAdapter(Protocol):
    @property
    def manifest(self) -> ProviderAdapterManifest: ...

    async def fetch(
        self, query: PlannedSearchQuery, page_token: str | None
    ) -> RawProviderResponse: ...

    def parse(self, query: PlannedSearchQuery, body: bytes) -> ParsedProviderPage: ...


class SearchFailureRecord(KnowledgeModel):
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    logical_query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_id: str = Field(min_length=1, max_length=80)
    stage: SearchFailureStage
    kind: SearchFailureKind
    error_class: str = Field(min_length=1, max_length=256)
    error_detail_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    retryable: bool
    status_code: int | None = Field(default=None, ge=100, le=599)
    occurred_at: AwareDatetime

    @property
    def failure_sha256(self) -> str:
        return content_sha256(self)


class ProviderPageReceipt(KnowledgeModel):
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    logical_query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_id: str = Field(min_length=1, max_length=80)
    adapter_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_index: int = Field(ge=0, le=10_000)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_page_token_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    output_page_token_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    outcome: QueryOutcome
    response: ArchivedProviderResponse | None = None
    parsed_page_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    terminal: bool | None = None
    query_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    failure_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def _receipt_matches_its_outcome(self) -> "ProviderPageReceipt":
        if self.response is not None:
            if self.response.request_sha256 != self.request_sha256:
                raise ValueError("archived response is bound to a different request")
            if self.response.logical_query_sha256 != self.logical_query_sha256:
                raise ValueError("archived response is bound to a different logical query")
        if self.outcome is QueryOutcome.SUCCESS:
            if (
                self.response is None
                or self.parsed_page_sha256 is None
                or self.terminal is None
                or self.failure_sha256 is not None
            ):
                raise ValueError("successful page requires response, parse, and terminal evidence")
        elif self.failure_sha256 is None:
            raise ValueError("failed page requires an exact failure record")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class SearchExecutionBundle(KnowledgeModel):
    schema_version: Literal[1] = 1
    execution_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    plan: SearchExecutionPlan
    session: SearchSession
    page_receipts: tuple[ProviderPageReceipt, ...] = Field(min_length=1)
    failures: tuple[SearchFailureRecord, ...]
    coverage_disposition: Literal["eligible", "blocked"]
    completed_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _execution_is_closed_and_fail_closed(self) -> "SearchExecutionBundle":
        if self.session.protocol_sha256 != self.plan.protocol.protocol_sha256:
            raise ValueError("search session is bound to a different protocol")
        if self.session.corpus_snapshot_sha256 != self.plan.protocol.corpus_snapshot_sha256:
            raise ValueError("search session is bound to a different corpus")
        if self.completed_at != self.session.ended_at:
            raise ValueError("execution completion must equal the session boundary")
        if len(self.page_receipts) > self.plan.protocol.max_queries:
            raise ValueError("executed page requests exceed the frozen query budget")
        query_records = {query.query_id: query for query in self.session.queries}
        receipt_ids = [receipt.request_id for receipt in self.page_receipts]
        if receipt_ids != [query.query_id for query in self.session.queries]:
            raise ValueError("page receipts must exactly preserve search-query record order")
        if len(receipt_ids) != len(set(receipt_ids)):
            raise ValueError("page request IDs must be unique")
        plan_hashes = {query.logical_query_sha256 for query in self.plan.queries}
        receipts_by_logical: dict[str, list[ProviderPageReceipt]] = defaultdict(list)
        for receipt in self.page_receipts:
            record = query_records[receipt.request_id]
            if receipt.query_record_sha256 != record.query_sha256:
                raise ValueError("page receipt is bound to a different search-query record")
            if receipt.logical_query_sha256 not in plan_hashes:
                raise ValueError("page receipt references a query outside the frozen plan")
            receipts_by_logical[receipt.logical_query_sha256].append(receipt)
            if receipt.outcome is QueryOutcome.SUCCESS:
                if record.outcome is not QueryOutcome.SUCCESS:
                    raise ValueError("successful receipt has a failed search-query record")
                assert receipt.response is not None
                if record.response_sha256 != receipt.response.response_sha256:
                    raise ValueError("search-query response hash differs from its archive receipt")
            elif record.outcome is not QueryOutcome.ERROR:
                raise ValueError("failed receipt has a successful search-query record")
        if set(receipts_by_logical) != plan_hashes:
            raise ValueError("every frozen logical query must produce at least one receipt")
        for receipts in receipts_by_logical.values():
            indexes = [receipt.page_index for receipt in receipts]
            if indexes != list(range(len(indexes))):
                raise ValueError("provider page receipts must be contiguous and ordered")

        failures = {failure.failure_sha256: failure for failure in self.failures}
        if len(failures) != len(self.failures):
            raise ValueError("search failures must be unique")
        referenced_failures = {
            receipt.failure_sha256
            for receipt in self.page_receipts
            if receipt.failure_sha256 is not None
        }
        if referenced_failures != set(failures):
            raise ValueError("page receipts must exactly close the failure ledger")
        for receipt in self.page_receipts:
            if receipt.failure_sha256 is None:
                continue
            failure = failures[receipt.failure_sha256]
            record = query_records[receipt.request_id]
            if (
                failure.request_id != receipt.request_id
                or failure.request_sha256 != receipt.request_sha256
                or record.error_class != failure.error_class
                or record.error_detail_sha256 != failure.error_detail_sha256
            ):
                raise ValueError("failure, receipt, and query record do not describe one event")

        closed = not self.failures and all(
            receipts[-1].outcome is QueryOutcome.SUCCESS and receipts[-1].terminal is True
            for receipts in receipts_by_logical.values()
        )
        expected_disposition = "eligible" if closed else "blocked"
        if self.coverage_disposition != expected_disposition:
            raise ValueError("search coverage disposition does not match execution closure")
        if bool(self.failures) != (
            self.session.stopping_reason is SearchStoppingReason.HARD_FAILURE
        ):
            raise ValueError("search stopping reason must expose every hard coverage failure")
        return self

    @property
    def execution_sha256(self) -> str:
        return content_sha256(self)


class CommittedSearchExecution(KnowledgeModel):
    schema_version: Literal[1] = 1
    execution: SearchExecutionBundle
    ledger: ArchivedSearchLedger

    @model_validator(mode="after")
    def _ledger_commits_the_exact_execution(self) -> "CommittedSearchExecution":
        payload = canonical_json_bytes(self.execution)
        if self.ledger.object_sha256 != self.execution.execution_sha256:
            raise ValueError("search ledger names a different execution identity")
        if self.ledger.ledger_sha256 != hashlib.sha256(payload).hexdigest():
            raise ValueError("search ledger hash does not commit the execution JSON")
        if self.ledger.ledger_bytes != len(payload):
            raise ValueError("search ledger byte count does not commit the execution JSON")
        return self


class ReplayReceiptAudit(KnowledgeModel):
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{1,191}$")
    status: ReplayItemStatus
    response_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SearchReplayAudit(KnowledgeModel):
    schema_version: Literal[1] = 1
    execution_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_manifest_sha256s: tuple[str, ...] = Field(min_length=2)
    receipts: tuple[ReplayReceiptAudit, ...] = Field(min_length=1)
    status: ReplayAuditStatus
    audited_at: AwareDatetime
    state: Literal["complete"] = "complete"

    @model_validator(mode="after")
    def _audit_status_matches_items(self) -> "SearchReplayAudit":
        ids = [receipt.request_id for receipt in self.receipts]
        if len(ids) != len(set(ids)):
            raise ValueError("replay audit request IDs must be unique")
        statuses = {receipt.status for receipt in self.receipts}
        expected = (
            ReplayAuditStatus.MISMATCH
            if ReplayItemStatus.MISMATCH in statuses
            else ReplayAuditStatus.INCOMPLETE
            if ReplayItemStatus.UNAVAILABLE in statuses
            else ReplayAuditStatus.COMPLETE
        )
        if self.status is not expected and not (
            self.status is ReplayAuditStatus.INCOMPLETE
            and expected is ReplayAuditStatus.COMPLETE
        ):
            raise ValueError("replay audit status does not match its receipt evidence")
        return self

    @property
    def audit_sha256(self) -> str:
        return content_sha256(self)


def _token_sha256(token: str | None) -> str | None:
    if token is None:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _request_id(query: PlannedSearchQuery, page_index: int) -> str:
    return f"{query.logical_query_id}:p{page_index:04d}"


def _request_sha256(
    *,
    plan: SearchExecutionPlan,
    query: PlannedSearchQuery,
    manifest: ProviderAdapterManifest,
    page_index: int,
    input_page_token_sha256: str | None,
) -> str:
    return content_sha256(
        {
            "plan_sha256": plan.plan_sha256,
            "logical_query_sha256": query.logical_query_sha256,
            "adapter_manifest_sha256": manifest.manifest_sha256,
            "page_index": page_index,
            "input_page_token_sha256": input_page_token_sha256,
        }
    )


def _error_class(error: Exception) -> str:
    return f"{type(error).__module__}.{type(error).__qualname__}"[:256]


def _error_detail_sha256(
    *,
    error: Exception,
    stage: SearchFailureStage,
    kind: SearchFailureKind,
    status_code: int | None,
) -> str:
    message_sha256 = hashlib.sha256(str(error).encode("utf-8", errors="replace")).hexdigest()
    return content_sha256(
        {
            "error_class": _error_class(error),
            "message_sha256": message_sha256,
            "stage": stage.value,
            "kind": kind.value,
            "status_code": status_code,
        }
    )


def _classify_error(
    error: Exception, *, default_stage: SearchFailureStage
) -> tuple[SearchFailureStage, SearchFailureKind, bool, int | None]:
    if isinstance(error, ProviderFetchError):
        return error.stage, error.kind, error.retryable, error.status_code
    if isinstance(error, ResponsePolicyViolation):
        return (
            SearchFailureStage.ARCHIVE,
            SearchFailureKind.POLICY_VIOLATION,
            False,
            None,
        )
    if isinstance(error, ResponseArchiveError):
        return SearchFailureStage.ARCHIVE, SearchFailureKind.ARCHIVE_ERROR, False, None
    if default_stage is SearchFailureStage.PARSE:
        return default_stage, SearchFailureKind.PARSE_ERROR, False, None
    if default_stage is SearchFailureStage.PACING:
        return default_stage, SearchFailureKind.PACING_FAILURE, True, None
    return default_stage, SearchFailureKind.UNEXPECTED_ERROR, False, None


class SearchExecutor:
    """Sequentially execute every planned source/query and never silently skip a failure."""

    def __init__(
        self,
        *,
        archive: ContentAddressedResponseArchive,
        adapters: Mapping[str, SearchProviderAdapter],
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.archive = archive
        self.adapters = dict(adapters)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleeper = sleeper

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("search execution clock must return timezone-aware datetimes")
        return value

    def _validate_adapters(self, plan: SearchExecutionPlan) -> None:
        if set(self.adapters) != set(plan.protocol.planned_source_ids):
            raise ValueError("executor adapters must exactly match frozen planned sources")
        for manifest in plan.adapters:
            adapter = self.adapters[manifest.source_id]
            if adapter.manifest != manifest:
                raise ValueError("runtime adapter manifest differs from the frozen search plan")

    def _failure_event(
        self,
        *,
        plan: SearchExecutionPlan,
        query: PlannedSearchQuery,
        manifest: ProviderAdapterManifest,
        page_index: int,
        request_sha256: str,
        input_token_sha256: str | None,
        response: ArchivedProviderResponse | None,
        parsed_page: ParsedProviderPage | None,
        error: Exception,
        default_stage: SearchFailureStage,
        occurred_at: datetime,
    ) -> tuple[SearchQueryRecord, ProviderPageReceipt, SearchFailureRecord]:
        stage, kind, retryable, status_code = _classify_error(
            error, default_stage=default_stage
        )
        request_id = _request_id(query, page_index)
        detail_sha256 = _error_detail_sha256(
            error=error, stage=stage, kind=kind, status_code=status_code
        )
        failure = SearchFailureRecord(
            request_id=request_id,
            logical_query_sha256=query.logical_query_sha256,
            request_sha256=request_sha256,
            source_id=query.source_id,
            stage=stage,
            kind=kind,
            error_class=_error_class(error),
            error_detail_sha256=detail_sha256,
            response_sha256=(response.response_sha256 if response is not None else None),
            retryable=retryable,
            status_code=status_code,
            occurred_at=occurred_at,
        )
        record = SearchQueryRecord(
            query_id=request_id,
            family=query.family,
            source_id=query.source_id,
            query_text=query.query_text,
            filters_sha256=query.filters_sha256,
            round_index=query.round_index,
            executed_at=occurred_at,
            outcome=QueryOutcome.ERROR,
            error_class=failure.error_class,
            error_detail_sha256=failure.error_detail_sha256,
        )
        receipt = ProviderPageReceipt(
            request_id=request_id,
            logical_query_sha256=query.logical_query_sha256,
            source_id=query.source_id,
            adapter_manifest_sha256=manifest.manifest_sha256,
            parser_sha256=manifest.parser_sha256,
            page_index=page_index,
            request_sha256=request_sha256,
            input_page_token_sha256=input_token_sha256,
            output_page_token_sha256=(
                _token_sha256(parsed_page.next_page_token)
                if parsed_page is not None
                else None
            ),
            outcome=QueryOutcome.ERROR,
            response=response,
            parsed_page_sha256=(
                parsed_page.page_sha256 if parsed_page is not None else None
            ),
            terminal=(parsed_page.terminal if parsed_page is not None else None),
            query_record_sha256=record.query_sha256,
            failure_sha256=failure.failure_sha256,
            recorded_at=occurred_at,
        )
        return record, receipt, failure

    async def execute(
        self,
        *,
        plan: SearchExecutionPlan,
        execution_id: str,
    ) -> SearchExecutionBundle:
        self._validate_adapters(plan)
        started_at = self._now()
        records: list[SearchQueryRecord] = []
        receipts: list[ProviderPageReceipt] = []
        failures: list[SearchFailureRecord] = []
        last_request_at: dict[str, datetime] = {}

        for query in plan.queries:
            manifest = next(
                item for item in plan.adapters if item.source_id == query.source_id
            )
            adapter = self.adapters[query.source_id]
            page_token: str | None = None
            seen_papers: set[str] = set()
            for page_index in range(query.max_pages):
                input_token_sha256 = _token_sha256(page_token)
                request_sha256 = _request_sha256(
                    plan=plan,
                    query=query,
                    manifest=manifest,
                    page_index=page_index,
                    input_page_token_sha256=input_token_sha256,
                )
                previous = last_request_at.get(query.source_id)
                if previous is not None:
                    elapsed = (self._now() - previous).total_seconds()
                    delay = manifest.minimum_request_interval_seconds - elapsed
                    if delay > 0:
                        try:
                            await self.sleeper(delay)
                        except Exception as error:
                            occurred_at = self._now()
                            record, receipt, failure = self._failure_event(
                                plan=plan,
                                query=query,
                                manifest=manifest,
                                page_index=page_index,
                                request_sha256=request_sha256,
                                input_token_sha256=input_token_sha256,
                                response=None,
                                parsed_page=None,
                                error=error,
                                default_stage=SearchFailureStage.PACING,
                                occurred_at=occurred_at,
                            )
                            records.append(record)
                            receipts.append(receipt)
                            failures.append(failure)
                            break
                last_request_at[query.source_id] = self._now()

                try:
                    raw_value = await adapter.fetch(query, page_token)
                    raw = RawProviderResponse.model_validate(raw_value)
                except Exception as error:
                    occurred_at = self._now()
                    record, receipt, failure = self._failure_event(
                        plan=plan,
                        query=query,
                        manifest=manifest,
                        page_index=page_index,
                        request_sha256=request_sha256,
                        input_token_sha256=input_token_sha256,
                        response=None,
                        parsed_page=None,
                        error=error,
                        default_stage=SearchFailureStage.TRANSPORT,
                        occurred_at=occurred_at,
                    )
                    records.append(record)
                    receipts.append(receipt)
                    failures.append(failure)
                    break

                received_at = self._now()
                if not 200 <= raw.status_code < 300:
                    kind = (
                        SearchFailureKind.RATE_LIMITED
                        if raw.status_code == 429
                        else SearchFailureKind.PROVIDER_ERROR
                    )
                    error = ProviderFetchError(
                        f"provider returned HTTP {raw.status_code}",
                        kind=kind,
                        stage=SearchFailureStage.PROVIDER,
                        retryable=raw.status_code in {408, 425, 429} or raw.status_code >= 500,
                        status_code=raw.status_code,
                    )
                    record, receipt, failure = self._failure_event(
                        plan=plan,
                        query=query,
                        manifest=manifest,
                        page_index=page_index,
                        request_sha256=request_sha256,
                        input_token_sha256=input_token_sha256,
                        response=None,
                        parsed_page=None,
                        error=error,
                        default_stage=SearchFailureStage.PROVIDER,
                        occurred_at=received_at,
                    )
                    records.append(record)
                    receipts.append(receipt)
                    failures.append(failure)
                    break

                try:
                    response = self.archive.store_response(
                        payload=raw.body,
                        media_type=raw.media_type,
                        manifest=manifest,
                        query=query,
                        request_sha256=request_sha256,
                        received_at=received_at,
                    )
                except Exception as error:
                    record, receipt, failure = self._failure_event(
                        plan=plan,
                        query=query,
                        manifest=manifest,
                        page_index=page_index,
                        request_sha256=request_sha256,
                        input_token_sha256=input_token_sha256,
                        response=None,
                        parsed_page=None,
                        error=error,
                        default_stage=SearchFailureStage.ARCHIVE,
                        occurred_at=received_at,
                    )
                    records.append(record)
                    receipts.append(receipt)
                    failures.append(failure)
                    break

                try:
                    parsed_value = adapter.parse(query, raw.body)
                    parsed = ParsedProviderPage.model_validate(parsed_value)
                    if len(parsed.hits) > query.max_results:
                        raise ValueError("provider page exceeds its frozen result limit")
                    if (
                        manifest.pagination_kind is PaginationKind.NONE
                        and not parsed.terminal
                    ):
                        raise ValueError("non-paginated adapter returned a continuation token")
                except Exception as error:
                    record, receipt, failure = self._failure_event(
                        plan=plan,
                        query=query,
                        manifest=manifest,
                        page_index=page_index,
                        request_sha256=request_sha256,
                        input_token_sha256=input_token_sha256,
                        response=response,
                        parsed_page=None,
                        error=error,
                        default_stage=SearchFailureStage.PARSE,
                        occurred_at=received_at,
                    )
                    records.append(record)
                    receipts.append(receipt)
                    failures.append(failure)
                    break

                duplicate_papers = seen_papers & {
                    hit.paper_snapshot_sha256 for hit in parsed.hits
                }
                if duplicate_papers:
                    error = ProviderFetchError(
                        "provider pagination repeated a paper identity",
                        kind=SearchFailureKind.DUPLICATE_PAGE_HIT,
                        stage=SearchFailureStage.PAGINATION,
                        retryable=False,
                    )
                    record, receipt, failure = self._failure_event(
                        plan=plan,
                        query=query,
                        manifest=manifest,
                        page_index=page_index,
                        request_sha256=request_sha256,
                        input_token_sha256=input_token_sha256,
                        response=response,
                        parsed_page=parsed,
                        error=error,
                        default_stage=SearchFailureStage.PAGINATION,
                        occurred_at=received_at,
                    )
                    records.append(record)
                    receipts.append(receipt)
                    failures.append(failure)
                    break
                seen_papers.update(hit.paper_snapshot_sha256 for hit in parsed.hits)

                if not parsed.terminal and page_index + 1 == query.max_pages:
                    error = ProviderFetchError(
                        "provider pagination did not terminate within the frozen page budget",
                        kind=SearchFailureKind.PAGINATION_INCOMPLETE,
                        stage=SearchFailureStage.PAGINATION,
                        retryable=False,
                    )
                    record, receipt, failure = self._failure_event(
                        plan=plan,
                        query=query,
                        manifest=manifest,
                        page_index=page_index,
                        request_sha256=request_sha256,
                        input_token_sha256=input_token_sha256,
                        response=response,
                        parsed_page=parsed,
                        error=error,
                        default_stage=SearchFailureStage.PAGINATION,
                        occurred_at=received_at,
                    )
                    records.append(record)
                    receipts.append(receipt)
                    failures.append(failure)
                    break

                request_id = _request_id(query, page_index)
                record = SearchQueryRecord(
                    query_id=request_id,
                    family=query.family,
                    source_id=query.source_id,
                    query_text=query.query_text,
                    filters_sha256=query.filters_sha256,
                    round_index=query.round_index,
                    executed_at=received_at,
                    outcome=QueryOutcome.SUCCESS,
                    hits=parsed.hits,
                    response_sha256=response.response_sha256,
                )
                receipt = ProviderPageReceipt(
                    request_id=request_id,
                    logical_query_sha256=query.logical_query_sha256,
                    source_id=query.source_id,
                    adapter_manifest_sha256=manifest.manifest_sha256,
                    parser_sha256=manifest.parser_sha256,
                    page_index=page_index,
                    request_sha256=request_sha256,
                    input_page_token_sha256=input_token_sha256,
                    output_page_token_sha256=_token_sha256(parsed.next_page_token),
                    outcome=QueryOutcome.SUCCESS,
                    response=response,
                    parsed_page_sha256=parsed.page_sha256,
                    terminal=parsed.terminal,
                    query_record_sha256=record.query_sha256,
                    recorded_at=received_at,
                )
                records.append(record)
                receipts.append(receipt)
                if parsed.terminal:
                    break
                page_token = parsed.next_page_token

        ended_at = max([self._now(), started_at, *(record.executed_at for record in records)])
        replay_cache: list[str] = []
        for record in records:
            if record.response_sha256 is not None and record.response_sha256 not in replay_cache:
                replay_cache.append(record.response_sha256)
        stopping_reason = (
            SearchStoppingReason.HARD_FAILURE
            if failures
            else SearchStoppingReason.SOURCE_EXHAUSTED
        )
        session = SearchSession(
            session_id=execution_id,
            protocol_sha256=plan.protocol.protocol_sha256,
            corpus_snapshot_sha256=plan.protocol.corpus_snapshot_sha256,
            queries=tuple(records),
            started_at=started_at,
            ended_at=ended_at,
            stopping_reason=stopping_reason,
            stopping_evidence_sha256=content_sha256(
                {
                    "plan_sha256": plan.plan_sha256,
                    "request_count": len(receipts),
                    "failure_sha256s": [failure.failure_sha256 for failure in failures],
                    "all_planned_queries_attempted": len(
                        {receipt.logical_query_sha256 for receipt in receipts}
                    )
                    == len(plan.queries),
                }
            ),
            replay_cache_sha256s=tuple(replay_cache),
        )
        return SearchExecutionBundle(
            execution_id=execution_id,
            plan=plan,
            session=session,
            page_receipts=tuple(receipts),
            failures=tuple(failures),
            coverage_disposition="blocked" if failures else "eligible",
            completed_at=ended_at,
        )

    async def execute_and_commit(
        self,
        *,
        plan: SearchExecutionPlan,
        execution_id: str,
    ) -> CommittedSearchExecution:
        execution = await self.execute(plan=plan, execution_id=execution_id)
        ledger = self.archive.store_ledger(
            value=execution,
            object_sha256=execution.execution_sha256,
            archived_at=self._now(),
        )
        return CommittedSearchExecution(execution=execution, ledger=ledger)


def load_search_execution(
    *, archive: ContentAddressedResponseArchive, ledger: ArchivedSearchLedger
) -> SearchExecutionBundle:
    payload = archive.read_ledger(ledger)
    execution = SearchExecutionBundle.model_validate_json(payload)
    if execution.execution_sha256 != ledger.object_sha256:
        raise ResponseArchiveCorruption("search ledger object identity changed")
    if canonical_json_bytes(execution) != payload:
        raise ResponseArchiveCorruption("search ledger is not canonical execution JSON")
    return execution


def _replay_evidence(
    *, request_id: str, status: ReplayItemStatus, detail: object
) -> str:
    return content_sha256(
        {"request_id": request_id, "status": status.value, "detail": detail}
    )


def replay_search_execution(
    *,
    execution: SearchExecutionBundle,
    archive: ContentAddressedResponseArchive,
    adapters: Mapping[str, SearchProviderAdapter],
    audited_at: AwareDatetime,
) -> SearchReplayAudit:
    adapter_map = dict(adapters)
    if set(adapter_map) != set(execution.plan.protocol.planned_source_ids):
        raise ValueError("replay adapters must exactly match frozen planned sources")
    manifest_by_source = {item.source_id: item for item in execution.plan.adapters}
    query_by_hash = {
        item.logical_query_sha256: item for item in execution.plan.queries
    }
    record_by_id = {item.query_id: item for item in execution.session.queries}
    failure_by_hash = {item.failure_sha256: item for item in execution.failures}
    audit_items: list[ReplayReceiptAudit] = []

    for receipt in execution.page_receipts:
        manifest = manifest_by_source[receipt.source_id]
        adapter = adapter_map[receipt.source_id]
        query = query_by_hash[receipt.logical_query_sha256]
        if adapter.manifest != manifest:
            status = ReplayItemStatus.MISMATCH
            detail: object = "adapter_manifest_drift"
        elif receipt.response is None:
            status = ReplayItemStatus.UNAVAILABLE
            detail = "request_has_no_archived_response"
        else:
            try:
                body = archive.read_response(receipt.response)
            except ResponseArchiveError as error:
                status = ReplayItemStatus.MISMATCH
                detail = {
                    "archive_error_class": _error_class(error),
                    "archive_error_sha256": hashlib.sha256(
                        str(error).encode("utf-8", errors="replace")
                    ).hexdigest(),
                }
            else:
                failure = (
                    failure_by_hash.get(receipt.failure_sha256)
                    if receipt.failure_sha256 is not None
                    else None
                )
                try:
                    parsed = ParsedProviderPage.model_validate(adapter.parse(query, body))
                except Exception as error:
                    if (
                        failure is not None
                        and failure.kind is SearchFailureKind.PARSE_ERROR
                        and _error_class(error) == failure.error_class
                    ):
                        status = ReplayItemStatus.VERIFIED
                        detail = "deterministic_parse_failure_reproduced"
                    else:
                        status = ReplayItemStatus.MISMATCH
                        detail = {
                            "parser_error_class": _error_class(error),
                            "parser_error_sha256": hashlib.sha256(
                                str(error).encode("utf-8", errors="replace")
                            ).hexdigest(),
                        }
                else:
                    record = record_by_id[receipt.request_id]
                    expected_output_token = _token_sha256(parsed.next_page_token)
                    if receipt.outcome is QueryOutcome.SUCCESS:
                        matches = (
                            receipt.parsed_page_sha256 == parsed.page_sha256
                            and receipt.terminal == parsed.terminal
                            and receipt.output_page_token_sha256 == expected_output_token
                            and record.hits == parsed.hits
                        )
                    elif failure is not None and failure.kind in {
                        SearchFailureKind.DUPLICATE_PAGE_HIT,
                        SearchFailureKind.PAGINATION_INCOMPLETE,
                    }:
                        matches = (
                            receipt.parsed_page_sha256 == parsed.page_sha256
                            and receipt.terminal == parsed.terminal
                            and receipt.output_page_token_sha256 == expected_output_token
                        )
                    else:
                        matches = False
                    status = (
                        ReplayItemStatus.VERIFIED
                        if matches
                        else ReplayItemStatus.MISMATCH
                    )
                    detail = {
                        "parsed_page_sha256": parsed.page_sha256,
                        "expected_parsed_page_sha256": receipt.parsed_page_sha256,
                    }
        audit_items.append(
            ReplayReceiptAudit(
                request_id=receipt.request_id,
                status=status,
                response_sha256=(
                    receipt.response.response_sha256
                    if receipt.response is not None
                    else None
                ),
                evidence_sha256=_replay_evidence(
                    request_id=receipt.request_id, status=status, detail=detail
                ),
            )
        )

    statuses = {item.status for item in audit_items}
    status = (
        ReplayAuditStatus.MISMATCH
        if ReplayItemStatus.MISMATCH in statuses
        else ReplayAuditStatus.INCOMPLETE
        if ReplayItemStatus.UNAVAILABLE in statuses
        or execution.coverage_disposition == "blocked"
        else ReplayAuditStatus.COMPLETE
    )
    return SearchReplayAudit(
        execution_sha256=execution.execution_sha256,
        adapter_manifest_sha256s=tuple(
            manifest.manifest_sha256 for manifest in execution.plan.adapters
        ),
        receipts=tuple(audit_items),
        status=status,
        audited_at=audited_at,
    )


__all__ = [
    "CircuitOpenError",
    "CommittedSearchExecution",
    "ParsedProviderPage",
    "ProviderFetchError",
    "ProviderPageReceipt",
    "RawProviderResponse",
    "ReplayAuditStatus",
    "ReplayItemStatus",
    "ReplayReceiptAudit",
    "SearchExecutionBundle",
    "SearchExecutor",
    "SearchFailureKind",
    "SearchFailureRecord",
    "SearchFailureStage",
    "SearchProviderAdapter",
    "SearchReplayAudit",
    "load_search_execution",
    "replay_search_execution",
]
