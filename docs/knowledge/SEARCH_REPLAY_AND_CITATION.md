# F8-S2 deterministic search, replay, citation, and coverage guide

## Current boundary

F8-S2 is a provider-neutral evidence harness. It can freeze a multi-source search plan, execute
injected adapters, retain metadata-only structured responses by content hash, record every page and
failure, replay the exact parser, expand backward/forward citations mechanically, stop under a
frozen saturation/budget rule, and derive the search-controlled portion of `CoverageReport`.

It is intentionally not connected to `ExperimentDriver` or the current SURVEY path. The repository
contains no production OpenAlex, Semantic Scholar, Crossref, or arXiv F8 adapter and no credentials.
The included adapters and provider bytes are synthetic test fixtures. No command in this guide
performs live retrieval.

## Evidence flow

```text
SearchProtocol + QueryTermSet + ProviderAdapterManifest[]
                         |
                         v
                SearchExecutionPlan
                         |
                         v
provider response -> metadata policy -> content-addressed response
                         |                     |
                         v                     v
            SearchQueryRecord + ProviderPageReceipt
                         |
                         v
        SearchExecutionBundle -> immutable execution ledger
                         |
                         v
            same-manifest/parser replay audit
                         |
                         v
all new citation hits -> derived citation-round plan -> ...
                         |
                         v
             CitationTraversalCampaign
                         |
                         v
external six signals + four derived signals -> CoverageReport
```

The last arrow does not produce a novelty judgment. The isolated F8-S3 claim extraction/review
harness is documented in [CLAIM_EXTRACTION_AND_REVIEW.md](CLAIM_EXTRACTION_AND_REVIEW.md), and the
isolated F8-S4 relation matcher is documented in
[PRIOR_ART_MATCHING_AND_REVIEW.md](PRIOR_ART_MATCHING_AND_REVIEW.md). F8-S5 real calibration and
novelty acceptance remain required.

## 1. Freeze the search inputs

Create the existing immutable `SearchProtocol` with:

- the exact corpus and cutoff;
- candidate-claim and initial-seed hashes;
- every required query family and at least two planned sources;
- maximum page requests and results per request;
- a frozen saturation rule and perturbation identity;
- `query_planner_sha256=QUERY_PLANNER_IDENTITY_SHA256`;
- `failure_policy=record_and_fail_hard_coverage`.

Build a `QueryTermSet` with `build_query_term_set`. The deterministic builder must provide quest,
mechanism, object, method, dataset, result, synonym, adjacent-field, and negation terms. Model terms
are accepted only as `model_supplement` for synonym or adjacent-field families and require the exact
generator manifest hash.

Freeze one `ProviderAdapterManifest` per planned source in protocol order. The manifest is part of
the scientific evidence, not runtime configuration. It includes code/parser/schema/terms hashes,
query capabilities, page/byte budgets, pacing, structured media types, and explicit exclusion of
abstract/body/full-text fields.

Create and freeze `CitationTraversalPolicy` before the initial plan. Pass its `policy_sha256` to
`build_search_execution_plan`; traversal refuses a policy added after the initial search.

## 2. Adapter contract

An F8 adapter exposes exactly:

```python
manifest: ProviderAdapterManifest
async fetch(query, page_token) -> RawProviderResponse
parse(query, body) -> ParsedProviderPage
```

`fetch` returns exact structured bytes, a media type, status code, and a hash of the selected
response headers. It must not return credentials. `parse` returns ordered `SearchHit` identities,
one continuation token or terminal state, and optional provider total. Search hits point to
`PaperSnapshot` hashes; provider records must be normalized/ingested before they can become evidence.

The executor verifies the runtime manifest before any request. Adapters should raise
`ProviderFetchError` or `CircuitOpenError` for classified failures. Unknown exceptions are still
recorded, but are non-retryable unexpected failures in the F8 ledger.

Official provider behavior must be reflected in the manifest and adapter tests:

- OpenAlex deep paging follows `next_cursor` to terminal; backward edges come from
  `referenced_works`, and forward edges use `cites`.
- Semantic Scholar citations and references are separate paginated endpoints.
- Crossref cursor termination uses returned-item count, because a cursor can remain present on the
  final page; cursor expiry and 429 must be errors.
- arXiv repeated calls need the documented delay and caching. Its ordinary Atom response includes
  `summary`, so it does not satisfy this metadata-only raw-response archive without a separately
  reviewed projection or content permission.

See the primary documentation linked in [ADR 0011](../adr/0011-f8-deterministic-search-replay-and-citation-traversal.md).

## 3. Response archive

Construct `ContentAddressedResponseArchive(root)`. The root itself must be a real directory, not a
symlink. The archive accepts objects up to 64 MiB globally and applies the smaller adapter limit.

Responses are stored as:

```text
responses/<sha[0:2]>/<sha[2:4]>/<sha>.response
```

Canonical ledgers use:

```text
ledgers/<sha[0:2]>/<sha[2:4]>/<sha>.json
```

Files are created exclusively and made read-only. Every read checks regular-file type, exact byte
count, and SHA-256. A missing, symlinked, truncated, extended, or changed file raises
`ResponseArchiveCorruption`.

Before writing a response, the archive validates media type and parses JSON/XML. Any occurrence of
`abstract`, `summary`, `body`, `full_text`, `fulltext`, or `source_text` as a field/tag rejects the
response. Error bodies are not archived as successful metadata. F8-S1 source text remains outside
this store.

## 4. Execute and commit

Use `SearchExecutor.execute_and_commit`, not a provider loop:

```python
executor = SearchExecutor(archive=archive, adapters=adapters)
committed = await executor.execute_and_commit(
    plan=plan,
    execution_id="f8-search-2026-08-14",
)
```

The executor sends requests sequentially and enforces the per-source minimum interval. Every
attempt creates a page receipt. It continues later planned queries after a failure, so the ledger
shows the complete attempted scope, but any failure blocks coverage.

Failure kinds include circuit-open, transport, rate-limited/provider status, access-policy/archive,
parse, duplicate cross-page paper, and pagination not terminal within budget. A failed query has no
hits. Raw error strings are not serialized; only class and derived detail hash are retained.

`load_search_execution` reopens the canonical execution ledger, validates every nested object, and
rehashes its identity. An in-memory execution that has not been committed is not sufficient evidence
for a later scientific gate.

## 5. Replay

Call `replay_search_execution` with the same source-to-adapter mapping. Replay:

1. checks every runtime manifest against the plan;
2. reads and rehashes each archived response;
3. runs the frozen parser implementation;
4. compares parsed-page hash, ordered hits, terminal state, and continuation-token hash;
5. marks response-less requests unavailable and any changed bytes/output a mismatch.

`complete` means every original request succeeded and every captured response reproduced. A
deterministically reproduced parse failure is `verified` at the receipt level but the overall audit
remains `incomplete`; it does not become a usable search result.

## 6. Citation traversal

`run_citation_traversal` starts from an initial execution whose plan already binds the traversal
policy. It commits the initial execution if necessary and then:

- unions all successful forward/backward hits across sources;
- removes already reached papers;
- sorts every new paper SHA-256;
- derives a complete next-round plan for every new paper, both directions, and every capable source;
- commits and replays that execution;
- repeats until saturation, complete frontier exhaustion, hard failure, or a pre-round budget stop.

Budgets are checked against a whole next round. The harness never spends the remaining budget on an
order-dependent subset of the frontier. Each derived plan binds its parent execution, complete
frontier, positive round index, and unchanged policy hash.

The campaign is coverage-eligible only after `saturation` or `source_exhausted`, with complete
execution ledgers and replay audits. `budget_exhausted`, any provider error, unavailable response,
or replay mismatch is blocked.

## 7. Coverage derivation

`build_f8_search_coverage_assessment` accepts a complete citation campaign, a frozen
`CoveragePolicy`, and six independently measured observations:

- known-answer recall;
- seed-reference recovery;
- full-text availability;
- source-span verification;
- correction/retraction-check completion;
- perturbation stability.

It derives four values that callers cannot override:

- query-family coverage: required families complete on every planned capable source;
- source diversity: planned sources whose full initial query set completed;
- citation-frontier saturation: one only for eligible saturation/exhaustion;
- uncovered-source fraction: planned sources with any incomplete query.

The policy must make these hard `1.0`, `1.0`, `1.0`, and `0.0` thresholds. All campaign sessions are
combined into one aggregate `SearchSession`, and `CoverageReport` derives its own hard-failure list
and verdict. `commit_f8_search_coverage_assessment` stores the complete assessment canonically.

Passing these four search-controlled signals is necessary, not sufficient. The six independent
signals still need real benchmark/corpus measurements; synthetic values are test fixtures only.

## 8. Verification

Run the isolated F8 suite:

```bash
conda run -n aletheia pytest -q tests/knowledge
conda run -n aletheia ruff check aletheia/knowledge tests/knowledge
```

F8-S2-specific tests are:

- `test_search_planning.py`;
- `test_response_archive.py`;
- `test_search_execution.py`;
- `test_citation_traversal.py`;
- `test_search_coverage.py`.

They use no internet and no real paper text. Adversarial cases cover model replacement of core
terms, secret filters, text-bearing responses, symlink/tamper attacks, adapter drift, open circuits,
429, parse failures, cross-page duplicates, unterminated cursors, request pacing, response-less
replay, derived-round failure, partial-round budgets, policy drift, caller-forged coverage, and
permissive thresholds.

## Explicit limits

- No live provider adapter or provider credential/configuration exists in F8-S2.
- The current SURVEY implementation and process-local cache are unchanged.
- There is no PDF/HTML/JATS/OCR extraction or licensed text archive here.
- Search hits do not automatically create corpus papers or source spans.
- No known-answer recall, temporal false-novelty, or domain-expert calibration has been run.
- This search harness itself produces no claim extraction, prior-art relation, novelty
  classification, SOTA result, direction decision, or manuscript statement. F8-S3 is a separate
  synthetic-only downstream harness and is not wired here.
- API replay proves captured-response processing, not index exhaustiveness or legal permission.
