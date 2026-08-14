# F8-S2 deterministic search, replay, and citation traversal implementation report

Date: 2026-08-14

## Outcome

The isolated F8-S2 evidence harness is engineering-complete. Aletheia can now freeze deterministic
multi-source query families, restrict model contributions to additive synonym/adjacent terms, bind
each provider and parser implementation, archive structured metadata responses by content hash,
record every successful or failed page, replay exact bytes through the same parser identity,
mechanically expand backward/forward citations, stop under a frozen saturation or whole-round
budget rule, and derive the search-controlled portion of `CoverageReport`.

This does not make the current autonomous run novelty-aware. No F8 live provider adapter exists;
the current mutable SURVEY/provider path is unchanged; all new provider responses are synthetic;
and the harness is not imported by the scheduler, API, memory service, direction gate, claim gate,
or write-up. F8-S3 claim extraction, F8-S4 prior-art matching, F8-S5 real calibration/novelty
acceptance, and a reviewed production cutover remain necessary.

## Delivered

### Frozen deterministic planning

[`aletheia/knowledge/search.py`](../aletheia/knowledge/search.py) adds:

- `SearchTerm` / `QueryTermSet`, requiring deterministic quest, mechanism, object, method, dataset,
  result, synonym, adjacent-field, and negation axes;
- `ProviderAdapterManifest`, freezing adapter/parser/schema/terms hashes, fields, media types,
  capabilities, pagination, page/byte budgets, request interval, retrieval permission, and failure
  semantics;
- `PlannedSearchQuery` and `SearchExecutionPlan`, binding exact terms, filters, cutoff, sources,
  initial seeds, both citation directions, query/page budgets, and planner identity;
- deterministic initial and mechanically derived citation-round plan builders.

Model terms are allowed only as supplemental synonym or adjacent-field terms with a generator
manifest. Missing deterministic core terms, changed planner identity, reordered/drifted adapters,
secret-bearing filters, unsupported query families, incomplete seed coverage, and worst-case page
budget overflow are rejected before execution.

### Metadata-only content-addressed archive

[`aletheia/knowledge/response_archive.py`](../aletheia/knowledge/response_archive.py) implements a
write-once response/ledger archive. Provider bytes use exclusive creation and content-derived paths;
reads verify regular-file type, exact size, and SHA-256. The archive rejects symlink roots/targets,
unsafe paths, empty/oversized objects, and tampering. Successful objects are `fsync`ed and mode
`0400`; identical content is safely deduplicated.

The archive parses structured JSON/XML and rejects `abstract`, Atom `summary`, `body`,
`full_text`, `fulltext`, and `source_text` fields/tags. Adapter manifests must explicitly exclude the
same fields. This implements the F8-S1 default that metadata may be retained while abstract/full
text needs separate article-level permission. It does not store documents or error bodies.

### Complete execution and failure ledger

[`aletheia/knowledge/search_execution.py`](../aletheia/knowledge/search_execution.py) adds the
injected adapter protocol, sequential/paced executor, page receipts, classified failure records,
execution bundle, canonical commit/load, and replay audit.

Every planned logical query produces at least one receipt. Multi-page requests carry hashed input
and output continuation tokens without persisting opaque tokens. Successful pages bind ordered
hits, exact response, parser identity, terminal state, and query record. Failures cover request
pacing, open circuit, transport, HTTP 429/provider status, metadata policy/archive, parse, repeated
paper across pages, and pagination that exceeds its frozen maximum.

The executor continues later queries after one source fails, preserving the full attempted scope,
but any failure makes coverage blocked and the session `hard_failure`. Failed queries have no hits;
error messages enter only a derived hash. `execute_and_commit` stores the complete query/failure
ledger canonically.

Replay refuses adapter-manifest drift, rehashes every response, reruns the parser, and compares hits,
parse-page identity, terminal state, and continuation hash. Missing responses are incomplete;
changed bytes/output are mismatches. Reproducing a parse failure verifies what happened but does not
turn that request into coverage.

### Mechanical two-direction citation traversal

[`aletheia/knowledge/citation_traversal.py`](../aletheia/knowledge/citation_traversal.py) freezes a
`CitationTraversalPolicy` and constructs a parent-linked campaign:

- round zero uses every initial seed in both directions on every capable source;
- all unique new hit hashes, without model/caller filtering, form the next sorted frontier;
- a derived plan binds the prior execution hash, unchanged policy, full frontier, round, sources,
  directions, and budgets;
- every round commits its execution ledger and receives a replay audit;
- marginal new-paper fraction and cumulative closure are immutable round evidence.

The campaign can end as saturated, source-exhausted, budget-exhausted before a partial round,
maximum-round-unsaturated, or hard failure. Only replay-complete saturation or source exhaustion is
coverage-eligible.

### Fail-closed search coverage

[`aletheia/knowledge/search_coverage.py`](../aletheia/knowledge/search_coverage.py) combines every
campaign execution into one aggregate `SearchSession` and derives:

- required query families complete on every applicable source;
- planned sources whose complete initial query set terminated;
- replayed citation saturation/source exhaustion;
- uncovered planned-source fraction.

Callers cannot submit those four observations. The F8-S2 builder only accepts policies with hard
thresholds `1.0`, `1.0`, `1.0`, and `0.0`. It separately requires evidence-bearing known-answer,
seed-reference, full-text, span-verification, correction/retraction, and perturbation observations,
then lets the existing `CoverageReport` derive statuses, hard failures, and verdict. The complete
assessment also has canonical commit/load support.

This is coverage plumbing, not F8-S5 calibration: the six independent observations in tests are
synthetic and no threshold has been scientifically validated on a real literature corpus.

## Official API and rights constraints incorporated

The design review used primary provider documentation:

- OpenAlex cursor termination and citation graph fields:
  [paging](https://developers.openalex.org/guides/page-through-results),
  [citation recipes](https://developers.openalex.org/guides/recipes);
- Semantic Scholar's separate paginated citation/reference endpoints:
  [Academic Graph API](https://api.semanticscholar.org/api-docs/graph);
- Crossref cursor terminal behavior, expiry, caching, status/backoff, and current access limits:
  [API tips](https://www.crossref.org/documentation/retrieve-metadata/rest-api/tips-for-using-the-crossref-rest-api/),
  [access and authentication](https://crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/);
- arXiv offset paging, documented pacing, and cache guidance:
  [API manual](https://info.arxiv.org/help/api/user-manual.html);
- Crossref's warning that some abstracts may remain copyrighted:
  [REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/).

These facts are frozen as adapter-manifest fields and fail-closed archive behavior, not assumed from
provider names. In particular, an ordinary arXiv Atom feed contains `summary` and therefore cannot
enter the metadata-only raw-response archive without a separate licensed-content decision or an
explicit deterministic projection design.

The complete architectural decision is in
[`ADR 0011`](adr/0011-f8-deterministic-search-replay-and-citation-traversal.md), and the developer
workflow is in
[`docs/knowledge/SEARCH_REPLAY_AND_CITATION.md`](knowledge/SEARCH_REPLAY_AND_CITATION.md).

## Adversarial evidence

Five F8-S2 test modules add **36 tests**:

- [`test_search_planning.py`](../tests/knowledge/test_search_planning.py);
- [`test_response_archive.py`](../tests/knowledge/test_response_archive.py);
- [`test_search_execution.py`](../tests/knowledge/test_search_execution.py);
- [`test_citation_traversal.py`](../tests/knowledge/test_citation_traversal.py);
- [`test_search_coverage.py`](../tests/knowledge/test_search_coverage.py).

| Threat | Control | Adversarial evidence |
|---|---|---|
| Model replaces a core search axis | deterministic term required for all nine axes | model-authored method term rejected; missing method rejected |
| Credential enters a frozen plan | secret-name filter denylist | `api_key` filter rejected |
| Abstract/full text mislabeled metadata | manifest exclusions plus structured payload scan | JSON abstract and Atom summary rejected before write |
| Archive path is substituted or changed | exclusive non-symlink write, size/hash readback | root/target symlink and byte tamper rejected |
| Runtime parser changes | exact adapter/parser manifest equality | drift rejected before first request |
| Open circuit appears as zero hits | explicit failure receipt and no failed-query hits | circuit recorded; all later queries still attempted; coverage blocked |
| Rate limit is ignored | HTTP 429 classified as retryable provider failure | 429 receipt blocks coverage |
| Parser failure discards provider evidence | response archived before parse | same parse failure replayed and remains incomplete |
| Provider repeats a page | cross-page paper identity ledger | duplicate second page blocks coverage |
| Cursor never terminates | exact maximum pages and terminal requirement | last non-terminal page becomes pagination failure |
| API is called too quickly | manifest interval and executor pacing | injected sleeper observes positive delays |
| Error leaks opaque provider value | message is double-bound through detail hash only | raw message and direct message hash absent from JSON |
| Citation frontier is cherry-picked | all new hits sorted into next plan | derived query seed set equals complete prior-round new set |
| Budget creates source-order sample | worst-case whole-round precheck | no partial derived execution when request/frontier budget is short |
| Round fails after initial success | per-round execution ledger plus replay | derived circuit failure hard-blocks campaign |
| Maximum rounds is called saturation | explicit unsaturated budget blocker | chain with new paper at last round is blocked |
| Caller supplies a fake saturation score | four signals are builder-derived only | caller-created citation signal observation rejected |
| Permissive policy hides an outage | exact hard search thresholds | `0.5` query-family threshold rejected |
| Campaign/result JSON is altered | canonical ledger commit/load | execution, every derived round, and assessment round-trip/re-hash |

## Verification

Focused verification at implementation completion:

- F8-S2 planning/archive/execution: **23 passed**;
- citation traversal: **7 passed**;
- coverage integration: **6 passed**;
- complete `tests/knowledge`: **66 passed**;
- Ruff over `aletheia/knowledge` and `tests/knowledge`: passed;
- Python compilation, public-export import, and `git diff --check`: passed;
- complete non-Docker project under controlled local PostgreSQL/data-source access:
  **733 passed, 1 skipped, 29 deselected** in **290.81 s**;
- complete real Docker isolation group: **29 passed, 734 deselected** in **26.62 s**.

The non-Docker total is exactly the F8-S1 baseline plus the 36 F8-S2 tests. Repository-wide
`ruff check .` is not clean: it reports 20 pre-existing findings in unrelated historical probe
scripts and one old test import. Those files were not changed or counted as F8 acceptance; the
entire changed F8 code/test scope passes Ruff.

## Limits and next work

- No real provider was contacted and no live API response is in the repository.
- No F8 adapter translates OpenAlex/Semantic Scholar/Crossref/arXiv into ingested `PaperSnapshot`
  objects yet.
- Metadata-only archived bytes do not prove provider-index correctness, exhaustiveness, or legal
  interpretation.
- No abstract/full-text/PDF/HTML/JATS/OCR archive or extractor is implemented.
- No known-answer set, temporal holdout false-novelty benchmark, query perturbation calibration, or
  domain-expert review has been run.
- No atomic claims are extracted from source spans; no nearest-prior-art relation or exact
  difference is computed.
- No novelty/SOTA/direction/write-up code consumes F8-S2, and no scientific claim is upgraded.

The next implementation slice is F8-S3 atomic claim extraction from exact licensed source spans,
with schema-first numeric/unit/population/condition fields, prompt-injection isolation, extraction
confidence, and independent review. F8-S5 must later calibrate the six independent coverage signals
and temporal false-novelty rate before any production novelty gate can be enabled.

Follow-up on 2026-08-15: that isolated F8-S3 extraction/review slice is now implemented; see
[`F8_S3_CLAIM_EXTRACTION_IMPLEMENTATION_REPORT_2026_08_15.md`](F8_S3_CLAIM_EXTRACTION_IMPLEMENTATION_REPORT_2026_08_15.md).
It does not alter F8-S2 search evidence or remove the need for live-provider/extractor calibration,
F8-S4 matching, F8-S5 temporal acceptance, and production wiring.
