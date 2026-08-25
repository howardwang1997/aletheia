# F8-S1 immutable corpus persistence implementation report

Date: 2026-08-14

## Outcome

The F8-S1 storage foundation is engineering-complete. Aletheia can now accept a provider-neutral,
license-explicit corpus-ingestion bundle, validate its temporal and article-level rights boundary,
persist normalized content-addressed rows and ordered membership edges transactionally, reject
mutation at the database layer, and reconstruct/re-hash the complete bundle on read.

This does not complete F8-S1 extraction/provider work or F8 scientific exit. There is no live
OpenAlex, Crossref, PMC, publisher, institutional, or user-upload adapter; no licensed raw-text
archive; and no current research run reads these tables. The existing mutable SURVEY path is
unchanged. Stored synthetic fixtures do not establish literature coverage, novelty, SOTA, or
autonomous frontier-scientist capability.

## Delivered

### License-explicit provider-neutral ingestion

[`aletheia/knowledge/ingestion.py`](../aletheia/knowledge/ingestion.py) adds immutable contracts for:

- `ContentAccessPolicy` — allowed access paths and individual uses, hash-only/encrypted retention,
  model-input explicitness, unknown-license ceiling, and freeze identity;
- `ContentAccessGrant` — one paper/source binding with exact content/license/terms/evidence hashes,
  metadata/abstract/full-text capability, open/institutional/user/metadata path, permitted uses,
  automation, redistribution, retention, observation, and expiry;
- `ProviderIngestReceipt` — provider/source version, raw-response hash, normalizer hash, paper, exact
  spans, grant, manual/automated mode, and fetch time;
- `CorpusIngestionBundle` — a closed policy/corpus/grant/receipt envelope and bundle hash.

Permission is non-transitive. Open or free access does not imply automated retrieval, model input,
retention, or redistribution. Unknown license evidence cannot authorize abstract/full-text
processing. Open-access text requires article-level evidence; institutional/user paths require the
corresponding proof. Every paper has exactly one grant and provider receipt, and each receipt must
cover its complete source-span set.

Source text and raw provider bytes are intentionally absent. The default is hash-only persistence.
This keeps restricted text out of database backups and operator output while retaining identities
that can verify separately authorized bytes later.

### Normalized immutable PostgreSQL layout

[`aletheia/knowledge/persistence.py`](../aletheia/knowledge/persistence.py) defines sixteen ORM tables
for policies, source versions, paper snapshots, paper text identities, source spans, publication
updates, access grants, provider receipts, corpus snapshots, ingestion bundles, and six ordered
membership edge sets.

Every scientific object uses its canonical SHA-256 primary key and stores validated JSON for exact
round-trip. Query-critical identities and times are indexed columns. Explicit membership rows
retain tuple ordinal, preserve deterministic hashes, and enforce foreign-key closure.

One `(canonical paper, publication version, text availability)` key can bind only one text hash.
Abstract and full-text scopes may coexist, but changed bytes in either scope require a new
publication version. Source `(source ID, snapshot ID)`, update ID, corpus `(snapshot ID, version)`,
grant ID, receipt ID, and bundle ID cannot be rebound silently.

Writes use PostgreSQL conflict-safe inserts. Exact retries are no-ops, and four simultaneous exact
writers converge on one bundle. A stable identity/content conflict raises
`ImmutableKnowledgeConflict`; the caller transaction rolls back every partial child insert.

Reads do not trust JSONB. They reload ordered children, validate every nested model, recompute every
child/corpus/policy/grant/receipt/bundle hash, and compare duplicated index columns. Missing,
dangling, reordered, or forged data fails closed.

### Alembic and database immutability

[`migrations/versions/20260814_0003_f8_corpus_persistence.py`](../migrations/versions/20260814_0003_f8_corpus_persistence.py)
is the repository's new single head. It creates all sixteen tables, constraints, indexes, and a
`BEFORE UPDATE OR DELETE` trigger on every table. Corrections and later observations are new inserts,
not edits. The local development PostgreSQL upgraded from `20260813_0002` to `20260814_0003`
successfully, and live migration/ORM comparison reports no differences.

`migrations/env.py` and `schema_migrations.py` now register F8 metadata. Legacy baseline adoption
still compares only the audited baseline and excludes all post-baseline tables; runtime startup
continues to fail closed when its schema revision is not the exact head.

### Offline operator boundary

[`scripts/manage_knowledge_corpus.py`](../scripts/manage_knowledge_corpus.py) provides:

- `validate <bundle.json>` — pure validation, hashing, and counts with no DB/network I/O;
- `persist <bundle.json>` — exact-head check plus transactional idempotent persistence;
- `inspect <bundle_sha256>` — full database reconstruction/revalidation and a safe summary.

The CLI accepts only regular non-symlink input up to 64 MiB. It never performs network retrieval or
prints source text. The workflow, permission matrix, layout, recovery rules, and exact commands are
in [`docs/knowledge/CORPUS_PERSISTENCE.md`](knowledge/CORPUS_PERSISTENCE.md).

The architectural decision and official rights/selector evidence are in
[`ADR 0010`](adr/0010-f8-immutable-corpus-persistence-and-access-rights.md).

## Threat-model traceability

| Threat | Control | Adversarial evidence |
|---|---|---|
| Public URL called reusable full text | explicit access class, capability, terms evidence, uses | unknown-license text rejected |
| Processing permission called redistribution | independent typed uses and derived boolean | forged redistribution rejected |
| Hash-only storage called retained content | retention/use consistency | retained-content bit rejected |
| Abstract called full text | exact capability/use rule | full-text use on abstract rejected |
| Manual license called automated access | automation flag plus receipt mode | automated receipt rejected |
| Expired access still used | receipt time checked against grant expiry | expiry-bound receipt rejected |
| Provider drops inconvenient spans | exact per-paper span set in receipt | missing span rejected |
| Provider output normalized differently later | raw-response and normalizer hashes | receipt content identity frozen |
| Same publication version changes bytes | separate immutable text-identity table | changed text transaction rejected |
| Same source snapshot label changes bytes | unique source/version identity | rebound source rejected |
| Exact retry duplicates evidence | conflict-safe content reconciliation | second write returns `created=false` |
| Concurrent writers fork membership | ordered conflict-safe edges | four writers converge to one bundle |
| Operator edits/deletes evidence | DB triggers on all sixteen tables | SQL update/delete rejected |
| Partial insert survives a failure | caller transaction owns all writes | forced abort leaves no bundle |
| JSON payload or edge order drifts | reconstruct, validate, and rehash on read | exact ordered round-trip checked |
| Paper prompt injection reaches logs/DB | no raw literature-text field | instruction-like fixture absent from JSONB |
| CLI follows substituted input | `lstat`, regular-file and symlink checks | symlink bundle rejected |

## Verification

Focused acceptance after implementation:

- issue-12 schema plus F8-S1 access/persistence/migration suite: **40 passed** in **1.26 s**;
- F8-S1 PostgreSQL/CLI tests alone: **11 passed** in **1.07 s**;
- real local Alembic upgrade `0002 -> 0003`: successful;
- live database tables are a superset of the sixteen expected names;
- Alembic autogeneration comparison between live DB and ORM metadata: **0 differences**;
- offline full migration SQL generation, single-head check, Ruff, compilation, JSON parsing, and
  `git diff --check`: passed before whole-project regression.
- complete non-Docker project under controlled local PostgreSQL/data-source access:
  **697 passed, 1 skipped, 29 deselected** in **296.07 s**;
- authoritative complete real Docker isolation rerun:
  **29 passed, 698 deselected** in **41.56 s**.

The first Docker pass had **28 passed, 1 failed** when a CORE-Bench environment probe container
printed its complete valid JSON but the Docker client did not exit cleanly. The exact failed test
passed alone immediately in **0.80 s**, and the full 29-test rerun then passed. This is recorded as a
container-runtime transient, not hidden or counted as the acceptance result.

## Limits and next work

- The store records raw-response hashes but does not retain replay bytes. A reviewed,
  content-addressed encrypted archive and revocation/expiry enforcement are separate work.
- Typed license evidence is auditable but is not legal proof or advice.
- Database owners can disable triggers and remain part of the trusted infrastructure boundary.
- No provider adapter yet verifies an external snapshot, retrieves licensed content, or emits this
  bundle in production.
- No PDF/HTML/JATS/OCR extractor yet produces the normalized selectors/spans.
- No search session, citation traversal, coverage report, claim graph, novelty assessment, SOTA
  comparator, direction gate, or write-up consumes persisted corpus rows.
- Immutable evidence retirement, legal hold, backup/restore, and encrypted-content key lifecycle
  need a dedicated operational policy.

The next engineering slice is F8-S2: deterministic query planning, multi-source response caching,
and forward/backward citation traversal over a frozen corpus. It must propagate retrieval failures
into hard coverage and cannot make novelty claims until F8-S3 through F8-S5 and temporal calibration
are complete.

Follow-up on 2026-08-14: the isolated F8-S2 harness is now implemented; see
[`F8_S2_SEARCH_REPLAY_CITATION_IMPLEMENTATION_REPORT_2026_08_14.md`](F8_S2_SEARCH_REPLAY_CITATION_IMPLEMENTATION_REPORT_2026_08_14.md).
It adds metadata-only replay bytes and query/citation ledgers without changing this report's
statement that there is no live provider, retained source text, production wiring, or novelty
capability.

Follow-up on 2026-08-15: the isolated F8-S3 claim extraction/review harness is now implemented; see
[`F8_S3_CLAIM_EXTRACTION_IMPLEMENTATION_REPORT_2026_08_15.md`](F8_S3_CLAIM_EXTRACTION_IMPLEMENTATION_REPORT_2026_08_15.md).
It consumes F8-S1 grants ephemerally and keeps source bytes outside persistent objects. It does not
add a production content resolver, retained licensed text, or novelty capability.
