# F8 issue 12 knowledge-boundary schema spike report

Date: 2026-08-14

## Outcome

F8 issue 12 is engineering-complete as the deliberately isolated ADR/fixture spike defined in the
master plan. Aletheia now has immutable, content-addressed contracts for temporal literature
snapshots, exact source-span evidence, replayable search, hard coverage, atomic claims, ranked prior
art, bounded novelty, correction/contradiction tracking, protocol comparability, and fail-closed
SOTA reporting.

This is not F8 scientific exit and is not connected to the autonomous research loop. No production
corpus was searched, no paper was classified as novel, no SOTA result was established, and no
driver, database, API, provider, or migration changed. The package exists to make later integration
prove an explicit evidence boundary instead of evolving the current mutable prose objects in place.

## Delivered

### Decision and evidence model

[`docs/adr/0009-f8-knowledge-boundary-schema-spike.md`](adr/0009-f8-knowledge-boundary-schema-spike.md)
records the temporal-holdout threat model, related-work evidence, immutable-object decision,
untrusted-literature boundary, hard-coverage semantics, novelty ceiling, exact SOTA protocol
identity, explicit exclusions, and rejected alternatives.

[`aletheia/knowledge/schemas.py`](../aletheia/knowledge/schemas.py) implements frozen Pydantic v1
contracts with unknown-field rejection and canonical SHA-256 identities. The package is exported by
[`aletheia/knowledge/__init__.py`](../aletheia/knowledge/__init__.py) and has no import path into the
existing driver.

The corpus layer distinguishes source update, retrieval, paper publication/version, observation,
extraction/review, cutoff, and freeze times. It verifies source/paper/span/update closure, license
identities, text availability, supersession, correction/retraction status, and reconstructed as-of
evidence. A later observation cannot silently enter a contemporaneous historical boundary.

### Replayable search and measured coverage

`SearchProtocol` freezes all core semantic query axes plus author and forward/backward citation
traversal, multiple sources, seed papers, query/round budgets, saturation, perturbation, failure
policy, and planner identity. `SearchSession` records success/error state, filters, ranked results,
timestamps, response hashes, and an exact replay-cache set.

`CoveragePolicy` requires all ten planned signals exactly once and freezes each direction,
threshold, and hard/soft status. `CoverageReport` derives the signal states, hard-failure list, and
overall verdict. The complete bundle rejects a sufficient verdict if any query records an outage or
the search stops on a hard failure.

### Claim graph and conservative novelty

`AtomicClaim` separates candidate and prior origins and decomposes subject, relation, object,
qualifiers, population, conditions, direction, type, effect, uncertainty, metric, and sample size.
Every prior claim in `AtomicClaimGraph` requires an exact `ClaimEvidenceEdge`; refuting and qualifying
edges remain first-class.

`PriorArtRelation` retains multi-channel retrieval evidence, exact component differences, rank,
matcher identity, evidence spans, and review. `NoveltyAssessment` binds the complete evidence package
and independently derives strong-claim eligibility. Missing coverage forces an indeterminate,
speculative result. Equivalent, subsuming, or special-case prior art, unresolved blockers, missing
differences, too few reviewers, a candidate-author reviewer, or an altered evidence hash prevents a
strong claim.

### Protocol-safe SOTA comparison

`DatasetVersion`, `MetricDefinition`, `MethodEntity`, `ResourceBudgetSignature`, and
`ProtocolSignature` provide canonical identities for the complete comparison protocol. The
comparator checks every required dimension and retains exact mismatch records. Evaluation time is
disclosed but non-blocking; task, data, split, leakage/grouping, preprocessing, exclusions, metric,
statistics, budget, external resource, and pretraining differences are blocking.

`SOTAComparison` recomputes raw and direction-normalized delta only for compatible protocols. A
non-comparable comparison cannot contain a delta, candidate-win bit, or headline permission even
when the contextual candidate score is numerically higher.

### Closed evidence bundle

`KnowledgeBoundarySnapshot` cross-validates all nested content identities. It verifies corpus and
cutoff bindings, source/query budgets, required search families, corpus-contained hits, coverage
timing, candidate/prior claim origins, source-span closure, prior-art evidence, novelty package and
policy minima, contradiction/update references, recomputed protocol comparisons, and final freeze
ordering. This prevents individually valid documents from being combined into a false bundle.

The developer-facing object guide and precise exclusions are in
[`docs/benchmarks/KNOWLEDGE_BOUNDARY_SCHEMA.md`](benchmarks/KNOWLEDGE_BOUNDARY_SCHEMA.md).

## Fixture and adversarial evidence

The pinned fixture
[`tests/knowledge/fixtures/knowledge_boundary_spike.v1.json`](../tests/knowledge/fixtures/knowledge_boundary_spike.v1.json)
uses only synthetic text and scores. It provides three pre-cutoff prior works, one post-cutoff
holdout, an instruction-like literature sentence, twelve query families over two source manifests,
three source-grounded prior claims, two independent reviewers, and compatible/split-mismatched
protocols. Both the fixture bytes and the final knowledge-snapshot identity are locked in tests.

[`tests/knowledge/test_schema_spike.py`](../tests/knowledge/test_schema_spike.py) contains 13 tests:

1. deterministic frozen bundle and unknown-field rejection;
2. post-cutoff paper leakage rejection;
3. reconstructed late observation without as-of evidence rejection;
4. invented full text and prompt-authority rejection;
5. mandatory exact evidence for every prior claim;
6. source outage degrading coverage and novelty;
7. equivalent prior art blocking strong novelty;
8. candidate-author self-review rejection;
9. evidence-package tamper rejection;
10. compatible versus split-mismatched SOTA behavior;
11. forged non-comparable win headline rejection;
12. corpus-external search-hit rejection;
13. correction-coverage/report consistency.

## Threat-model traceability

| Threat | Contract | Adversarial evidence |
|---|---|---|
| Future paper leaks into historical novelty test | publication/version/observation cutoff closure | future exact-match paper rejected |
| Live source reconstructs history without proof | reconstructed objects require as-of hashes | late observation rejected |
| Paywall/abstract is called full text | availability and span-scope closure | forged full-text span rejected |
| Paper text escalates model/tool authority | literal untrusted-data type; extra fields forbidden | instruction-like sentence stays a hash; authority field rejected |
| Retrieval outage looks like absence of prior art | hard coverage derived from recorded errors | outage forces insufficient/indeterminate |
| Summary drops a contradicting/qualifying source | typed evidence relations | graph retains exact typed edges |
| Prior art has no inspectable rationale | prior claims require span edges | missing edge rejected |
| Equivalent work is relabelled novel | exact blocking relation semantics | strong eligibility rejected |
| Candidate authors approve themselves | author/reviewer identity exclusion | self-review rejected |
| Review applies to edited evidence | evidence-package content hash | tampered hash rejected |
| Better number uses another split | per-dimension protocol comparison | split mismatch is non-comparable |
| Reporter still writes “beats SOTA” | non-comparable derived fields must be empty | forged delta/headline rejected |
| Valid files from different runs are combined | top-level cross-object hash closure | outside-corpus hit rejected |

## Verification

Focused verification after implementation:

- knowledge-schema spike: **13 passed**;
- Ruff format and checks pass for the new package/tests;
- package compilation and public-export import pass;
- fixture file SHA-256:
  `c58b1364ab99d9c4f184b5177051fe045dc5a832b91165e82a49c0e2e38c8d5c`;
- deterministic synthetic knowledge snapshot SHA-256:
  `857a235f99695acad0144728bf9c3f8ae62d920d77d24a3611060f196966c3a6`.
- complete non-Docker project under controlled local PostgreSQL/data-source access:
  **680 passed, 1 skipped, 29 deselected** in **294.32 s**;
- complete real Docker isolation group: **29 passed, 681 deselected** in **27.16 s**;
- final `git diff --check`, JSON parsing, package compile, and production-import-boundary audit pass.

## Limits and next F8 work

Issue 12 closes only the interface/fixture risk. F8-S1 through F8-S6 still need licensed source
adapters and text storage, immutable persistence/migrations, extraction and review queues, citation
traversal, deterministic query planning, known-answer calibration, multi-channel prior-art matching,
temporal false-novelty evaluation, direction/write-up gates, and production SOTA matrices.

The next implementation slice should be F8-S1: persist immutable `CorpusSnapshot`,
`PaperSnapshot`, publication updates, and `SourceSpan` objects behind a provider-neutral interface,
with license/access policy and snapshot/replay tests. It must preserve the issue-12 schemas and must
not declare a novelty capability merely because storage and ingestion exist.

Follow-up on 2026-08-14: that isolated F8-S1 storage foundation is now implemented under Alembic
`20260814_0003`; see
[`F8_S1_CORPUS_PERSISTENCE_IMPLEMENTATION_REPORT_2026_08_14.md`](F8_S1_CORPUS_PERSISTENCE_IMPLEMENTATION_REPORT_2026_08_14.md).
The driver/non-claim limits above remain in force.

The isolated F8-S2 deterministic search/replay/citation harness is also now implemented; see
[`F8_S2_SEARCH_REPLAY_CITATION_IMPLEMENTATION_REPORT_2026_08_14.md`](F8_S2_SEARCH_REPLAY_CITATION_IMPLEMENTATION_REPORT_2026_08_14.md).
Its metadata-only archive and fail-closed coverage derivation satisfy the spike's replay interface,
but live-source calibration, F8-S4 matching, F8-S5 acceptance, and production wiring remain absent.

Follow-up on 2026-08-15: the isolated F8-S3 licensed exact-span claim extraction/review harness is
now implemented; see
[`F8_S3_CLAIM_EXTRACTION_IMPLEMENTATION_REPORT_2026_08_15.md`](F8_S3_CLAIM_EXTRACTION_IMPLEMENTATION_REPORT_2026_08_15.md).
It satisfies the spike's strict atomic-field, evidence-edge, low-confidence review, contradiction,
and graph-closure interfaces with synthetic fixtures. It does not change the original issue-12
acceptance result or establish real extraction accuracy, prior-art matching, or novelty capability.
