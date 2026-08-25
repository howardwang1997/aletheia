# ADR 0011: Deterministic search, metadata-only replay, and mechanical citation traversal

- Status: Accepted
- Date: 2026-08-14
- Scope: F8-S2 / Knowledge Boundary Engine

## Context

The compatibility literature path in `aletheia/research/literature.py` queries Semantic Scholar,
arXiv, OpenAlex, and Crossref on a best-effort basis. It parses directly into mutable `Paper`
objects, retains only a process-local result cache, silently skips a source while its circuit is
open, and discards exact provider bytes and parser identity. This is useful enrichment, but it
cannot support a negative prior-art claim:

- a changed live result or parser cannot be replayed;
- a partial page, 429, timeout, parse error, or open circuit can look like an empty result;
- an LLM can omit a query family while producing plausible prose;
- forward/backward citation traversal has no frozen frontier or stopping evidence;
- provider metadata, abstracts, and full text are not separated by a retained-content policy.

Provider APIs also have materially different completeness semantics. OpenAlex recommends cursor
paging for deep results and requires following `next_cursor` until terminal; its citation recipes
use `referenced_works` for outgoing edges and `cites` for incoming edges:
[OpenAlex paging](https://developers.openalex.org/guides/page-through-results),
[OpenAlex citation recipes](https://developers.openalex.org/guides/recipes). Semantic Scholar has
separate paginated `/citations` and `/references` endpoints:
[Semantic Scholar Academic Graph API](https://api.semanticscholar.org/api-docs/graph). Crossref
cursors expire, may still be returned on the last page, and require a result-count terminal check;
it asks clients to cache, inspect status codes, and back off:
[Crossref API tips](https://www.crossref.org/documentation/retrieve-metadata/rest-api/tips-for-using-the-crossref-rest-api/),
[Crossref access and limits](https://crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/).
arXiv exposes offset paging, recommends a three-second delay between repeated calls, and asks
clients to cache stable daily results:
[arXiv API manual](https://info.arxiv.org/help/api/user-manual.html).

These rules cannot remain informal adapter comments. Search completeness has to be an immutable
input to later novelty decisions.

## Decision

### Keep F8-S2 isolated from the compatibility path

Add new `aletheia/knowledge` planning, response-archive, execution, citation, and coverage modules.
Do not change `ExperimentDriver`, SURVEY, the current provider functions, direction scoring, claim
strength, or write-up. A future cutover requires real-provider calibration and a separate ADR.

The new API accepts injected provider adapters and is exercised with synthetic metadata only. It
does not imply that any live source has been licensed, configured, or queried.

### Freeze core query axes before execution

`QueryTermSet` requires deterministic terms for quest, mechanism, object, method, dataset, result,
synonym, adjacent-field, and negation axes. An optional author axis is supported. Model-generated
terms can only supplement synonym or adjacent-field searches and must carry their generator
manifest hash. They cannot replace a deterministic core term.

`SearchExecutionPlan` binds the existing `SearchProtocol`, exact terms, source order, adapter
manifests, query strings, filters, cutoff, page/result budgets, seeds, and planner identity. The
initial plan covers every required family and both citation directions for every frozen seed on all
capable sources. A plan whose worst-case page requests exceed its frozen budget is invalid before
network I/O.

### Treat an adapter as a frozen evidence-producing program

`ProviderAdapterManifest` records source, adapter and parser hashes, response schema, terms,
supported query families, structured media types, included/excluded fields, pagination kind, page
size, maximum pages/bytes, minimum request interval, and automated-retrieval permission. Runtime
adapter drift is rejected before the first request.

The F8-S2 archive accepts only structured metadata responses. The adapter manifest must explicitly
exclude abstract/body/full-text fields, and the archive parses JSON/XML and rejects text-bearing
keys such as `abstract`, `body`, `full_text`, `source_text`, and Atom `summary`. This is stricter than
provider availability. Crossref explicitly warns that some deposited abstracts may remain subject
to copyright:
[Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/).

A future arXiv adapter cannot call the standard Atom feed a metadata-only raw response because it
contains `<summary>`. It must either have a policy permitting that content or archive an exact,
deterministic, separately identified metadata projection while retaining only the raw response hash.
F8-S2 deliberately implements neither exception.

### Use a write-once content-addressed archive

`ContentAddressedResponseArchive` writes provider bytes beneath their SHA-256, using exclusive
creation, bounded writes, file and directory `fsync`, read-only mode, non-symlink paths, exact byte
counts, and rehash-on-read. Identical bytes deduplicate; existing different or unsafe content fails
closed. Canonical execution and coverage ledgers use the same write-once primitive in a separate
namespace.

This archive stores metadata-only responses, not source documents. F8-S1 corpus text remains
hash-only unless a later access policy explicitly authorizes retained content.

### Record every request outcome, including skipped work

`SearchExecutor` executes the complete frozen plan sequentially per source and enforces each
manifest's minimum interval. Every page produces both a `SearchQueryRecord` and a
`ProviderPageReceipt`. Failures identify stage and class for pacing, circuit-open, transport, HTTP
429/provider response, policy/archive, parse, duplicate-page hit, or unterminated pagination.
Messages are represented only by a derived hash; opaque provider values do not enter the ledger.

An error never becomes an empty successful result. The executor continues with other planned
queries so that the ledger distinguishes one failed source from an abandoned campaign, but any
failure sets `coverage_disposition=blocked` and `SearchStoppingReason.HARD_FAILURE`.

### Replay with the same parser identity

Successful bytes are reloaded and rehashed, then passed to an adapter whose manifest and parser
hash exactly match the frozen plan. Parsed hits, terminal state, and hashed continuation token are
compared with the original receipt. A reproducible parse failure is itself verified evidence but
still leaves the execution incomplete. A request with no response is unavailable; changed bytes,
adapter drift, or changed parse output is a mismatch.

Only a complete execution with every receipt verified receives a complete replay audit. Query and
failure ledgers are themselves content-addressed and reload through schema validation plus canonical
rehashing.

### Derive citation frontiers mechanically

`CitationTraversalPolicy` freezes both directions, maximum rounds/requests/expanded papers,
saturation threshold/window, ascending paper-hash ordering, and the rule
`all_new_hits_no_model_selection`.

Round zero is the initial plan's seed queries. Every unique new citation hit becomes the next
frontier; neither a model nor caller can cherry-pick it. A later `citation_round` plan binds the
previous execution hash, the same policy hash, the complete sorted frontier, every capable source,
and both directions. Each round commits its own immutable execution ledger and replay audit.

Traversal stops as one of:

- `saturation` after the frozen marginal-new threshold/window;
- `source_exhausted` when the complete frontier produces no new papers;
- `budget_exhausted` before a partial unplanned round, or at the maximum round without saturation;
- `hard_failure` after any execution or replay failure.

Only saturation or complete source exhaustion without blockers is coverage-eligible.

### Derive search coverage instead of accepting a score

F8-S2 derives query-family coverage, complete-source fraction, citation saturation, and uncovered
source fraction from the campaign. Callers cannot supply these four values. The default coverage
policy fixes them as hard thresholds `1.0`, `1.0`, `1.0`, and `0.0`; a more permissive policy is
rejected rather than used to hide an outage.

Known-answer recall, seed-reference recovery, full-text availability, span verification,
correction/retraction checks, and perturbation stability remain externally measured inputs. The
builder requires all six exact evidence identities and derives status/verdict under the frozen
`CoveragePolicy`. It combines every round into one immutable aggregate `SearchSession` before
building `CoverageReport`.

## Consequences

- Aletheia now has an isolated, replayable F8-S2 search harness whose absence/failure semantics are
  suitable for later coverage calibration.
- API pagination, request pacing, response bytes, parser identity, failure state, citation frontier,
  stopping reason, and coverage derivation are independently auditable.
- Metadata-only response storage avoids silently retaining abstracts/full text but excludes provider
  formats that cannot meet that boundary.
- Full traversal can be storage- and request-intensive; budgets are frozen and incomplete campaigns
  remain blocked rather than partially sampled.
- No live adapter, known-answer benchmark, temporal false-novelty calibration, claim extraction,
  prior-art matcher, novelty acceptance, or production integration is delivered here.
- A complete replay proves deterministic processing of captured metadata, not that an external
  index was exhaustive, correct, or legally interpreted.

## Rejected alternatives

- **Keep the process-local result cache:** it cannot reproduce provider bytes after restart.
- **Archive only normalized `Paper` objects:** parser drift and dropped provider fields become
  invisible.
- **Treat circuit-open as a skipped source:** it converts infrastructure failure into apparent
  absence of prior art.
- **Let an LLM choose citation seeds each round:** it makes frontier omissions unauditable and
  potentially confirmation-biased.
- **Stop mid-round when a budget is reached:** source/order effects create an unregistered sample.
- **Store every raw API body under “metadata”:** some APIs include abstracts or text by default.
- **Average retrieval health into one score:** a missing hard source could be hidden by easy signals.
- **Wire the new engine into novelty now:** F8-S3 through F8-S5 and real calibration are still absent.

## Follow-up

On 2026-08-15, isolated F8-S3 claim extraction/review was implemented under
[ADR 0012](0012-f8-licensed-atomic-claim-extraction-and-independent-review.md). This does not change
F8-S2 provider/search evidence and does not authorize novelty wiring; F8-S4/F8-S5 and real
calibration remain absent.
