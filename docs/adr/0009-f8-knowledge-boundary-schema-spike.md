# ADR 0009: Immutable knowledge-boundary schemas before retrieval integration

- Status: Accepted for spike
- Date: 2026-08-14
- Scope: F8 / implementation issue 12

## Context

The current literature path intentionally treats search as best-effort enrichment. `Paper` is a
mutable normalized record; `LiteratureFinding` stores a paper key and prose fields; `SOTAResult`
stores a dataset, metric, score, and short split label. Those objects were enough to stop completely
ungrounded claims and to make early runs inspectable. They cannot establish a knowledge boundary:

- a paper version can change without creating a new identity;
- a claim points to a paper, not an exact source span;
- publication, indexing, correction, and observation times are not separated;
- retrieval outages and missing full text are averaged into a vague weak-survey flag;
- candidate and prior claims are not decomposed into comparable components;
- an LLM novelty scalar can hide equivalent or special-case prior art;
- SOTA rows do not bind dataset bytes, split, leakage rules, metric formula, resources, or date;
- the same mutable search can silently change an earlier ideation decision.

Issue 12 is deliberately a schema/fixture spike. It must define the evidence boundary before F8
search, extraction, matching, and driver wiring are implemented. It does not add migrations,
network providers, embeddings, a graph database, or a novelty claim to the production loop.

## Evidence from related work

SciFact frames scientific verification as selecting evidence that supports or refutes a claim and
identifying the rationale, which motivates typed evidence edges to exact spans rather than a paper
reference alone: [Wadden et al., 2020](https://aclanthology.org/2020.emnlp-main.609/).
SciFact-Open shows that open-corpus verification is materially harder and includes evidence that
supports only a special case, which motivates explicit `special_case`, qualification, and coverage
states: [Wadden et al., 2022](https://aclanthology.org/2022.findings-emnlp.347/).

S2ORC demonstrates the value of structured full text with resolved references and inline citation
links for replayable citation traversal: [Lo et al., 2020](https://arxiv.org/abs/1911.02782).
OpenAlex publishes partitioned snapshots with `updated_date` semantics, supporting corpus-version
identity rather than an unspecified live API view:
[OpenAlex snapshot format](https://developers.openalex.org/download/snapshot-format). Crossref
exposes post-publication updates, corrections, retractions, and relations, and recommends separate
update records rather than overwriting an original work:
[Crossref versioning guidance](https://www.crossref.org/documentation/principles-practices/best-practices/versioning/),
[REST filters](https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-filters/).

An emerging 2026 preprint reports a substantial disagreement between standalone LLM novelty judges
and domain experts on reconstructed research questions. It is not treated as settled evidence, but
it reinforces the conservative decision not to let a model scalar confer novelty:
[RQ-Bench preprint](https://arxiv.org/abs/2606.12071).

## Decision

### Isolated immutable package

Create `aletheia/knowledge/schemas.py` as pure, frozen Pydantic contracts using the existing
canonical JSON/SHA-256 function. Issue 12 adds no imports from this package to the scheduler,
research provider adapters, memory service, API, or database models. A synthetic fixture and tests
exercise the contracts in isolation.

The spike intentionally does not decide the final relational table layout. F8-S1 will translate
accepted objects into Alembic migrations after real query patterns and retention requirements are
reviewed. A graph database remains out of scope until a measured graph-query need exists.

### Versioned corpus and temporal integrity

`PaperSnapshot` represents one observed publication version, not a canonical paper that mutates in
place. It separates first-public, version-public, and corpus-observation times; binds metadata and
available text hashes; records license, peer-review state, text availability, and
correction/retraction links; and optionally points to the prior snapshot it supersedes.

`CorpusSnapshot` binds exact source snapshot versions, included paper snapshots and source spans,
license policy, cutoff time, and freeze time. A contemporaneous temporal-holdout snapshot accepts
only paper versions and observations at or before the cutoff. A paper published later, an update
observed later, or a live API result without an as-of identity cannot enter that boundary.

Corrections and retractions create new paper/update objects. They do not rewrite the historical
snapshot. A later knowledge snapshot can supersede the old decision while preserving what was
known when the earlier decision froze.

### Source spans are untrusted evidence objects

`SourceSpan` binds a paper-snapshot hash, abstract/full-text scope, normalized locator, exact and
normalized text hashes, byte count, extraction method, OCR/extraction confidence, and extraction
time. Literature text is always marked untrusted. Prompt instructions inside a paper never change
tool authority or schema state; future extractors must pass the text as delimited data.

Metadata-only access cannot manufacture a full-text span. OCR below the frozen confidence floor is
eligible for retrieval but not verified evidence until a second model or human reviews it. Exact
text remains in a separately licensed content store; the manifest and graph use content identity.

### Atomic claims and evidence edges

`AtomicClaim` decomposes subject, relation, object, qualifiers, population, conditions, direction,
claim type, and optional quantitative effect/uncertainty. Candidate and prior claims are distinct
origins. Every prior claim in a frozen graph requires at least one source-span edge.

`ClaimEvidenceEdge` uses `supports`, `refutes`, `qualifies`, or `mentions`, with extraction
confidence and reviewer status. Contradictory and qualifying evidence remains first-class; summary
generation cannot erase it. Human-verified status requires a reviewer identity and review time.

`PriorArtRelation` links one candidate claim to one prior claim as equivalent, subsuming,
special-case, extension, combination, or contradiction. It stores component-wise differences,
retrieval-channel scores, matcher identity, rank, and evidence spans. Equivalent/subsuming prior
art is a hard strong-novelty blocker. Contradiction is not automatically proof of novelty.

### Search replay and hard coverage

`SearchProtocol` freezes the candidate claims, corpus, cutoff, required query families, seed papers,
sources, citation traversal, budgets, stopping rule, perturbation plan, and planner identity before
execution. `SearchSession` records every query, filter hash, ranked result identity, error, round,
timestamp, and stopping reason. External API responses will later be cached as content-addressed
artifacts; the schema already binds those response hashes.

`CoveragePolicy` declares exact signal directions and hard/soft status. `CoverageReport` does not
accept an LLM-authored overall number. Its verdict is derived from known-answer recall, seed and
citation recovery, query-family coverage, source/date/venue diversity, full-text availability,
span verification, correction/retraction checks, perturbation stability, saturation, and uncovered
source fraction. Any missing or failed hard signal yields `coverage_insufficient`.

An outage is evidence about coverage, not evidence that no prior art exists.

### Novelty is a bounded classification

`NoveltyAssessment` uses the frozen corpus, search session, coverage report, atomic claim graph,
and ranked prior-art relations. Its classifications match the F8 plan: known equivalent, known
special case, incremental extension, novel combination, novel method, novel phenomenon,
contradictory to prior, or indeterminate due to coverage.

Insufficient coverage forces `indeterminate_due_to_coverage` and a speculative/unverified claim
ceiling. A strong-novelty-eligible classification requires sufficient coverage, the frozen minimum
number of nearest prior claims, component-wise exact differences, no unresolved equivalent or
subsuming blocker, explicit temporal/model-prior limitations, and author-excluded independent
review. Reviewers can request search or confirm the evidence package; they cannot relabel missing
coverage as novelty.

### Protocol identity before SOTA arithmetic

`ProtocolSignature` binds task definition, dataset content/version, split bytes, grouping/leakage
policy, preprocessing, exclusions, exact metric formula and aggregation, uncertainty/test policy,
compute and data budget, external resources/pretraining, and evaluation date.

`assess_protocol_comparability` compares each frozen dimension. A SOTA delta exists only when all
required dimensions match. Otherwise `SOTAComparison` is `non_comparable`, lists exact mismatches,
has no headline delta, and forbids a win claim. A better number under another split or metric
formula remains reportable as context but cannot become “beats SOTA.”

### Fixture boundary

The issue-12 fixture contains only synthetic short text and explicitly includes:

- one prior paper inside a temporal cutoff;
- an equivalent/special-case prior claim with an exact evidence span;
- one future paper that must remain outside the corpus and acts as temporal holdout;
- a complete search protocol plus a source-outage counterexample;
- a sufficient and insufficient coverage case;
- compatible and split-mismatched SOTA protocols;
- an instruction-like paper sentence that remains untrusted data.

The fixture proves schema behavior, not retrieval quality or novelty accuracy.

## Consequences

- Existing `Paper`, `LiteratureFinding`, and `SOTAResult` remain unchanged during the spike.
- F8 implementation must create new immutable versions rather than silently enrich old rows.
- Temporal holdout can test false novelty without leaking post-cutoff papers into the corpus.
- Search completeness and claim verification become separately measurable.
- Novelty and SOTA claims gain explicit fail-closed states instead of optimistic prose fallbacks.
- The richer schema costs more storage and requires exact provider-version/caching discipline.
- Human/expert review remains necessary for the strongest novelty class; the schema makes that
  dependency auditable rather than pretending to remove it.

## Rejected alternatives

- **Extend the current mutable `Paper` dataclass in place:** loses historical knowledge boundaries.
- **Store one embedding and nearest-paper score:** cannot represent support, refutation,
  qualification, conditions, or exact differences.
- **Use an LLM novelty score as the gate:** conflates confidence, style, and novelty and cannot
  convert missing coverage into evidence.
- **Average every coverage signal:** allows a missing hard source or zero verified spans to be
  hidden by easy soft components.
- **Compare only dataset/metric names for SOTA:** aliases can conceal different bytes, splits,
  leakage policies, formulas, and resource regimes.
- **Add a graph database now:** commits infrastructure before query and scale evidence exists.
- **Wire schemas directly into the driver in issue 12:** would turn a fixture spike into an
  unreviewed production migration and make rollback harder.
