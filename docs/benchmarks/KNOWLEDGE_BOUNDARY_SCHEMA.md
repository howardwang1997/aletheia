# F8 knowledge-boundary schema spike

Status: issue-12 engineering fixture, not a production retrieval pipeline

## What exists

`aletheia.knowledge` now defines immutable, content-addressed contracts for the evidence boundary
that a later F8 implementation must satisfy before it can make a novelty or SOTA claim. The package
has no scheduler, database, network-provider, model, tool, or research-driver imports.

The artifact dependency is deliberately one-way:

```text
versioned sources + papers + spans -> CorpusSnapshot
                                     -> SearchProtocol -> SearchSession -> CoverageReport
CorpusSnapshot + spans ------------> AtomicClaimGraph -> PriorArtRelation
all evidence above -----------------------------------> NoveltyAssessment
dataset + metric + protocols ------------------------> SOTAComparison
updates + contradictions ----------------------------> ContradictionCorrectionReport
all frozen artifacts --------------------------------> KnowledgeBoundarySnapshot
```

Each top-level object exposes a canonical SHA-256 identity. Models are frozen and reject unknown
fields. `KnowledgeBoundarySnapshot` checks cross-object identities rather than accepting a bag of
individually valid JSON documents.

## Boundary guarantees

### Corpus and time

- One `PaperSnapshot` is one observed publication version. Corrections and retractions create new
  objects rather than editing historical evidence.
- A contemporaneous `CorpusSnapshot` rejects versions, observations, source updates, and notices
  after its cutoff.
- A reconstructed snapshot may observe an older object later only when an explicit as-of evidence
  hash proves what existed at the cutoff.
- Metadata-only records cannot contain source spans, and abstract-only records cannot manufacture
  full-text spans.

### Literature is data, never authority

`SourceSpan` records only licensed-store content hashes, a normalized locator, extraction method,
confidence, verification, and the literal trust label `untrusted_literature_data`. It has no prompt,
role, permission, or tool-authority field. Extractors added later must preserve this separation.

### Search and coverage

The protocol freezes candidate claims, corpus/cutoff, deterministic query families, multiple
sources, citation traversal, seed papers, budgets, saturation rule, perturbations, and planner
identity before execution. The session retains every query, ranked hit, response hash, error, and
round. Its replay cache must exactly equal the successful response identities.

Coverage is not an averaged model score. The policy defines all ten signals exactly once. The
report derives each pass/fail state and the overall verdict from thresholds; a missing hard signal
is a hard failure. Within the complete bundle, any recorded retrieval error or hard-failure stop
cannot coexist with `coverage_sufficient`.

### Claims, prior art, and novelty

- Every prior-art atomic claim must have an evidence edge to an exact span.
- Support, refutation, qualification, and mention remain distinct edge types.
- Prior-art relations retain rank, multi-channel retrieval signals, component-wise differences,
  matcher identity, evidence, and review state.
- Equivalent, subsuming, and special-case relations are strong-novelty blockers.
- Insufficient coverage forces `indeterminate_due_to_coverage` and a `speculative` ceiling.
- Strong novelty additionally requires enough nearest prior art, exact differences, no blockers,
  two or more author-excluded independent reviews, and explicit temporal/model-prior disclosures.
- The evidence-package hash binds the policy, corpus, search, coverage, graph, candidate claims,
  ranked relations, and cutoff. Editing any member invalidates the assessment and its reviews.

The schema bounds what a claim may say. It does not decide whether the synthetic candidate is
actually novel and does not use an LLM scalar as novelty evidence.

### SOTA comparability

`ProtocolSignature` binds the task, exact dataset bytes/schema/version, split policy and bytes,
grouping/leakage rules, preprocessing, exclusions, metric formula/aggregation/direction, uncertainty
and statistical test, resource budget, external resources, pretraining, and evaluation date.

Method identities are expected to differ and are not themselves a mismatch. Evaluation-date
differences are disclosed but non-blocking. Every other dimension must match. A non-comparable
pair may retain its two contextual scores, but `SOTAComparison` requires all delta, outperformance,
and headline fields to remain empty/false. It is therefore invalid to turn a better number under a
different split into “beats SOTA.”

## Synthetic fixture

[`tests/knowledge/fixtures/knowledge_boundary_spike.v1.json`](../../tests/knowledge/fixtures/knowledge_boundary_spike.v1.json)
is intentionally small and contains no real scientific result. It includes:

- three pre-cutoff prior papers and exact source spans;
- one instruction-like sentence that remains untrusted literature data;
- one post-cutoff exact-match paper excluded as a temporal holdout;
- all required search axes, two sources, citation traversal, and cached responses;
- sufficient coverage plus a tested source-outage reconstruction;
- three ranked component-wise prior-art relations and two independent novelty reviews;
- one compatible SOTA pair and one pair with different split bytes.

The fixture file and resulting bundle have pinned hashes in the tests. A silent fixture or schema
change therefore cannot look like the same acceptance evidence.

Run the spike checks with the project Conda environment:

```bash
conda run -n aletheia pytest -q tests/knowledge/test_schema_spike.py
```

The tests cover time leakage, reconstruction without as-of evidence, invented full text, prompt
injection fields, missing claim evidence, retrieval outage, false strong novelty, author self-review,
tampered evidence packages, protocol mismatch, forged SOTA headlines, corpus-external hits, and an
incomplete correction report.

## F8-S1 follow-up now available

The issue-12 schemas remain pure and driver-independent. Follow-up F8-S1 adds a separate
`aletheia.knowledge.ingestion` contract and `aletheia.knowledge.persistence` store:

- explicit article-level access grants separate metadata/abstract/full-text capability, automated
  retrieval, model input, retention, and redistribution;
- provider receipts bind raw-response and normalizer hashes without storing source text;
- Alembic `20260814_0003` creates normalized object tables and ordered membership edges;
- PostgreSQL triggers reject updates/deletes and every read reconstructs and rehashes the bundle;
- `scripts/manage_knowledge_corpus.py` validates, persists, and inspects typed bundles offline.

See [`F8-S1 corpus persistence`](../knowledge/CORPUS_PERSISTENCE.md) and
[`ADR 0010`](../adr/0010-f8-immutable-corpus-persistence-and-access-rights.md). This follow-up does
not change the synthetic issue-12 verdict or wire novelty into a run.

## F8-S2 follow-up now available

F8-S2 adds the isolated implementation behind the spike's search/replay interfaces:

- deterministic query terms and frozen provider/parser manifests;
- metadata-only content-addressed responses plus canonical query/failure ledgers;
- explicit paging, pacing, circuit/429/parse/pagination failure semantics and same-parser replay;
- mechanical two-direction citation rounds with whole-round budgets and saturation evidence;
- an aggregate search session and four fail-closed coverage signals that callers cannot override.

See [`F8-S2 search/replay guide`](../knowledge/SEARCH_REPLAY_AND_CITATION.md) and
[`ADR 0011`](../adr/0011-f8-deterministic-search-replay-and-citation-traversal.md). The
implementation uses synthetic adapters only and remains outside the scheduler/novelty path.

## F8-S3 follow-up now available

F8-S3 adds the isolated implementation behind the spike's claim/evidence interfaces:

- frozen deterministic/model extractor and exact output-schema manifests with no tool authority;
- grant expiry plus separate span-extraction/model-input permission enforcement;
- ephemeral licensed canonical document/span bytes with exact and normalized hash/locator checks;
- strict atomic fields for numeric effects, units, population, conditions, uncertainty, and
  confidence;
- complete attempt/failure ledgers and mechanically derived OCR/low-confidence review work;
- independent human/second-model accept/revise/reject decisions;
- contradiction-preserving, exact-span-closed claim graphs;
- immutable execution, resolution, and graph ledgers plus derivation replay.

See [`F8-S3 claim extraction guide`](../knowledge/CLAIM_EXTRACTION_AND_REVIEW.md) and
[`ADR 0012`](../adr/0012-f8-licensed-atomic-claim-extraction-and-independent-review.md). All content
and extractors are synthetic fixtures; no production resolver or scientific claim is introduced.

## Explicitly absent

The repository still does not add:

- live Crossref/OpenAlex/Semantic Scholar/arXiv/publisher F8 adapters or licensed text storage;
- production PDF/HTML/JATS/OCR extraction, embeddings, calibrated real-corpus claim extraction, or
  prior-art matching;
- scheduler, direction-gate, scorecard, write-up, API, or dashboard wiring;
- calibrated known-answer recall, temporal false-novelty measurements, or a real novelty decision;
- a command that builds production `knowledge_snapshot.json`.

Those are F8-S4 through F8-S6 implementation and scientific-exit work. The issue-12 architecture,
evidence rationale, and rejected alternatives remain recorded in
[`ADR 0009`](../adr/0009-f8-knowledge-boundary-schema-spike.md).
