# ADR 0010: Immutable corpus persistence with explicit article-level access rights

- Status: Accepted for F8-S1
- Date: 2026-08-14
- Scope: F8-S1 corpus/source-span persistence; no research-driver integration

## Context

Issue 12 froze pure knowledge-boundary contracts but deliberately added no storage. Existing
`literature_findings` rows are mutable prose extractions keyed by a run and paper string. They cannot
round-trip a historical corpus, retain tuple order used by content hashes, distinguish source and
paper versions, enforce a source-span foreign key, or prove why abstract/full-text processing was
permitted.

The storage boundary must also avoid turning availability into a copyright conclusion. Crossref
states that most deposited metadata is reusable but warns that some abstracts remain subject to
publisher or author copyright; its full-text metadata provides URLs and intended-purpose/license
information rather than the content itself:
[Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/),
[Crossref text and data mining](https://www.crossref.org/documentation/retrieve-metadata/rest-api/text-and-data-mining/).
PMC likewise says that not every freely readable article permits automated retrieval or reuse,
limits systematic retrieval to named services, and requires article-specific license compliance:
[PMC Open Access Subset](https://pmc.ncbi.nlm.nih.gov/tools/openftlist/),
[PMC copyright notice](https://pmc.ncbi.nlm.nih.gov/about/copyright/).

For source-span location, the W3C Web Annotation recommendation defines both normalized text quote
and half-open text position selectors. It specifically notes that position selectors avoid copying
restricted source text, although they are brittle without a version/state identity. This supports
the existing combination of character positions, normalized-span hash, and immutable paper version:
[W3C Web Annotation Data Model](https://www.w3.org/TR/annotation-model/).

## Decision

### Keep F8-S1 separate from the current literature path

Add `aletheia/knowledge/ingestion.py` and `aletheia/knowledge/persistence.py`. Neither is imported by
the scheduler, API, memory service, current literature providers, direction gate, or write-up path.
Provider implementations added later must normalize into the pure ingestion contract; they cannot
write partially interpreted rows directly.

### Make access capabilities explicit and non-transitive

`ContentAccessPolicy` freezes allowed access classes and uses. `ContentAccessGrant` binds one paper
snapshot and source manifest to:

- metadata, abstract, or full-text capability;
- open-access, institutional, user-provided, or metadata-only access path;
- exact license and license-terms identities plus independent terms evidence;
- article-level, institutional-contract, user-attestation, or unknown license evidence;
- separate permissions for metadata indexing, abstract/full-text processing, span extraction,
  model input, content retention, and redistribution;
- automated-retrieval permission, retention class, observation, and optional expiry.

No permission implies another. A source labelled open or freely readable does not automatically
permit model input, retention, redistribution, or automated retrieval. Unknown license evidence
cannot authorize abstract/full-text processing. Open-access text requires article-level evidence;
institutional and user-provided paths require their corresponding proof.

The default is `hash_only`. The PostgreSQL store contains hashes, positions, normalized metadata,
grants, and receipt identities, not raw literature text. An encrypted content archive and key/expiry
service are intentionally outside F8-S1; a later implementation must satisfy the retained-content
permission before adding one.

### Bind provider normalization and replay identity

`ProviderIngestReceipt` binds a provider/source snapshot, provider record ID, raw-response hash,
normalizer hash, resulting paper and exact span identities, access grant, manual/automated mode, and
fetch time. The receipt does not claim that a hash-only response is retrievable. Later replay
storage must add a content-addressed artifact reference under the same access policy; until then,
absence of replay bytes is a measurable coverage limitation.

`CorpusIngestionBundle` requires exactly one grant and receipt for each paper, exact span coverage,
source closure, capability/license equality with the paper snapshot, policy subset compliance,
valid retrieval permission/expiry, and chronological closure. Its hash commits to the access
evidence as well as the corpus.

### Use normalized immutable PostgreSQL rows and ordered membership edges

Alembic revision `20260814_0003` adds sixteen tables:

- access policies, source versions, paper snapshots, paper text identities, source spans,
  publication updates, access grants, and provider receipts;
- corpus snapshots and ingestion bundles;
- four ordered corpus membership tables and two ordered bundle membership tables.

Each scientific object uses its canonical SHA-256 as primary key and retains its validated JSON
payload for exact reconstruction. Query-critical identities/timestamps are separate columns.
Membership is not an untyped JSON array: `(parent hash, member hash, ordinal)` rows preserve order,
foreign-key closure, and deterministic rehashing.

A `(canonical paper ID, publication version, text availability)` identity can bind only one text
content hash. A changed abstract/full text therefore requires a new publication version instead of
silently mutating an old one. Source `(source ID, snapshot ID)`, publication update ID, corpus
`(snapshot ID, version)`, grant ID, receipt ID, and bundle ID are likewise stable.

Every F8 table has a PostgreSQL `BEFORE UPDATE OR DELETE` trigger. Corrections, later observations,
new access evidence, and expanded membership are new inserts. This protects application and normal
operator paths; a database owner can still disable triggers and therefore remains inside the trusted
infrastructure boundary.

### Revalidate on every read

Writes use PostgreSQL conflict-safe inserts, reconcile exact retries, and run inside the caller's
transaction. Four simultaneous identical writers converge on one bundle. Stable-identity/content
conflicts raise `ImmutableKnowledgeConflict` and roll back the whole attempt.

Reads reconstruct children in stored ordinal order, validate every Pydantic payload, recompute every
object and bundle hash, verify duplicated index columns, and fail closed on a missing/dangling row.
The `manage_knowledge_corpus.py` CLI validates normalized JSON without database I/O, persists it only
at the exact Alembic head, and re-audits an existing bundle by hash. It refuses symlink inputs and
does no network retrieval.

## Consequences

- F8 now has a durable, deterministic corpus/source-span foundation without changing current run
  behavior or claiming novelty capability.
- Publication versions, corpus versions, and access evidence remain independently auditable.
- Copyrighted text does not leak into ordinary JSONB backups or logs by default.
- Later F8 stages can join spans and papers by content identity and can measure unavailable or
  non-replayable evidence instead of pretending it was searched.
- The schema adds sixteen tables and immutable triggers; retention and backup operations require an
  explicit future archival/retirement design rather than row deletion.
- The existing mutable `literature_findings` and `sota_results` tables remain for compatibility and
  are not upgraded into F8 evidence.
- Legal/contract review remains an external responsibility. Typed declarations make the claimed
  permission inspectable but cannot prove a license interpretation is legally correct.

## Rejected alternatives

- **Store only a complete corpus JSON blob:** loses relational closure, query indexes, ordered-edge
  auditing, and conflict isolation.
- **Normalize rows but allow updates:** destroys temporal holdouts and makes earlier novelty
  decisions non-reproducible.
- **Treat DOI/OpenAlex/Crossref availability as full-text permission:** conflates metadata,
  abstract, access, automated retrieval, model input, retention, and redistribution.
- **Store source text directly in JSONB:** exposes restricted content to backups, logs, and broad
  database readers and makes expiry/contract enforcement harder.
- **Deduplicate only on paper canonical ID:** collapses preprint, publication, correction, and
  observed versions into a mutable record.
- **Insert provider rows directly:** bypasses normalized receipt, license, cutoff, and hash closure.
- **Wire the new store into `ExperimentDriver` now:** would expose an uncalibrated persistence layer
  as a novelty feature before F8-S2 through F8-S5 exist.
