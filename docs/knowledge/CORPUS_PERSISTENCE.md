# F8-S1 immutable corpus persistence

Status: storage foundation available; provider and research-driver integration absent

## Purpose

F8-S1 persists a normalized `CorpusIngestionBundle` without storing raw literature text. The bundle
contains a frozen corpus, article-level access policy/grants, and provider normalization receipts.
Every object is content-addressed and every read recomputes its identity.

This store is not the current SURVEY implementation. `ExperimentDriver`, `literature_findings`, and
`sota_results` are unchanged. Persisting a corpus does not pass coverage, novelty, or SOTA gates.

## Database upgrade

Back up any non-disposable database first, following [`migrations/README.md`](../../migrations/README.md).
Then apply the single Alembic head:

```bash
conda run -n aletheia alembic upgrade head
conda run -n aletheia python -c \
  "from aletheia.db import require_schema_current; print(require_schema_current())"
```

The current repository head is `20260818_0021`; `20260814_0003` is the revision that introduced
this corpus ledger. Application startup fails closed when the database is behind or ahead of the
current repository head.

The migration creates sixteen `knowledge_*` tables. All reject SQL `UPDATE` and `DELETE`. Do not
disable those triggers as an application workaround. A changed publication, corrected record,
later source observation, access decision, or corpus membership must use a new version and hash.

## Normalized input contract

A provider adapter must construct one JSON-serializable `CorpusIngestionBundle` containing:

- `access_policy`: frozen allowed access classes, uses, retention, and unknown-license ceiling;
- `corpus`: exact source versions, paper versions, spans, updates, cutoff, and freeze;
- `access_grants`: exactly one per paper, with terms evidence and separate processing/model-input/
  retention/redistribution/automation rights;
- `provider_receipts`: exactly one per paper, binding raw-response hash, normalizer, paper, spans,
  access grant, retrieval mode, and fetch time.

There is intentionally no raw response or source-text field. F8-S2 now provides a separate
content-addressed archive for structured metadata search responses, but it is not linked to these
paper-ingestion receipts and explicitly rejects abstract/full-text fields. A hash-only ingestion
receipt proves the normalized object came from specific bytes if those bytes are independently
provided later; it does not claim that this ingestion response is presently replayable. Licensed
text retention still needs a separate reviewed archive whose controls satisfy the grant.

F8-S3 now provides a separate synthetic claim-extraction harness that consumes a validated
`CorpusIngestionBundle`, rechecks its grants, and receives source bytes ephemerally from an injected
resolver. It is not wired to these PostgreSQL rows and does not add retained licensed text. See
[`CLAIM_EXTRACTION_AND_REVIEW.md`](CLAIM_EXTRACTION_AND_REVIEW.md).

Generate machine-readable JSON Schema directly from the installed contract when implementing an
adapter:

```bash
conda run -n aletheia python -c \
  "import json; from aletheia.knowledge import CorpusIngestionBundle; print(json.dumps(CorpusIngestionBundle.model_json_schema(), indent=2))"
```

## Validate, persist, and inspect

Validation performs no database or network operation:

```bash
conda run -n aletheia python scripts/manage_knowledge_corpus.py \
  validate /absolute/path/to/corpus_ingestion_bundle.json
```

Persist only after reviewing the emitted bundle, corpus, policy, and count identities:

```bash
conda run -n aletheia python scripts/manage_knowledge_corpus.py \
  persist /absolute/path/to/corpus_ingestion_bundle.json
```

The insert is transactional and concurrency-safe. An exact retry returns `"created":false`. A
stable source/update/corpus/grant/receipt/bundle identity attached to different content fails and
rolls back. The CLI accepts only a regular non-symlink file no larger than 64 MiB.

Reconstruct and revalidate an existing bundle:

```bash
conda run -n aletheia python scripts/manage_knowledge_corpus.py \
  inspect <64-character-bundle-sha256>
```

`inspect` loads ordered membership edges, validates every nested Pydantic object, recomputes child,
corpus, policy, grant, receipt, and bundle hashes, and compares duplicated index columns. It returns
only a summary; raw literature content is never printed because it is not in the store.

## Access-rights rules

| Capability/path | Required evidence | What is not inferred |
|---|---|---|
| Metadata only | metadata-index use; terms-evidence hash | abstract/full text, model input |
| Abstract | exact content hash; abstract-processing use; known terms evidence | full-text processing |
| Open-access full text | article-level terms; full-text processing; span extraction | model input, retention, redistribution |
| Institutional text | article terms or institutional contract | automated retrieval or redistribution |
| User-provided text | article terms or user attestation | ownership, public redistribution |
| Automated retrieval | explicit automation flag plus receipt mode | permission from a public URL alone |
| Retention | `retain_content` plus encrypted-content retention class | permission from processing rights |
| Redistribution | explicit redistribution use and matching boolean | permission from retention or model input |

The schema captures the asserted evidence boundary; it is not legal advice and does not replace
license/contract review.

## Persistence layout

Object tables store canonical JSON plus query-critical columns. Ordered link tables preserve the
tuple order used by canonical hashing:

```text
knowledge_access_policies
knowledge_corpus_sources       knowledge_paper_snapshots
                               ├── knowledge_paper_text_identities
                               └── knowledge_source_spans
knowledge_publication_updates  knowledge_content_access_grants
knowledge_provider_receipts
              \                 /
               knowledge_corpus_snapshots
               ├── corpus source/paper/span/update members + ordinal
               └── knowledge_ingestion_bundles
                   └── bundle grant/receipt members + ordinal
```

Foreign keys prevent dangling membership. The separate paper text identity prevents one canonical
paper/version/text-scope key from acquiring new bytes; an abstract and full text may have separate
identities, but either changing requires a new publication version.

## Verification

Focused contract and database checks:

```bash
conda run -n aletheia pytest -q \
  tests/knowledge/test_ingestion_contract.py \
  tests/knowledge/test_corpus_persistence.py \
  tests/test_schema_migrations.py
```

The database tests require the configured local PostgreSQL. They cover migration/ORM parity,
ordered round-trip, exact retry, four concurrent writers, no raw text in JSONB, trigger-enforced
immutability, version conflicts, source-snapshot conflicts, missing objects, rollback, CLI
validation/persistence/inspection, and symlink refusal.

## Recovery and limits

- Do not delete immutable evidence to “retry.” Create a new bundle.
- A database owner can disable triggers; infrastructure administrators remain trusted.
- Backup/restore, long-term archival, access revocation, encrypted licensed-text storage, and legal
  hold/retirement need a dedicated follow-up design.
- `raw_response_sha256` is an ingestion replay identity, not proof that those bytes were retained;
  the separate F8-S2 metadata search archive does not change that fact.
- No live OpenAlex, Crossref, PMC, publisher, institutional, or user-upload adapter is included.
- No claim graph, coverage, novelty, SOTA, direction, or write-up gate reads these tables yet.

The architectural reasoning and alternatives are in
[`ADR 0010`](../adr/0010-f8-immutable-corpus-persistence-and-access-rights.md).
