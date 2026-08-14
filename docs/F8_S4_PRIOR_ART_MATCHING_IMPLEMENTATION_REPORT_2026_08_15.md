# F8-S4 prior-art matching implementation report

- Date: 2026-08-15
- Scope: multi-channel claim recall, complete-union reranking, strict relation/difference judgment,
  independent review, and immutable resolution
- Status: isolated engineering slice complete; real scientific calibration and integration absent

## Outcome

F8-S4 now implements a fail-closed path from an F8-S3 reviewed atomic-claim graph to auditable
nearest-prior-art relation candidates. It runs lexical, embedding, citation, and structured-entity
recall as separately frozen programs; retains every unique hit and channel receipt; requires
multi-channel agreement before making a formal relation; forces a reranker to score the complete
union without deletion; lets the harness derive final order and selection; and produces strict
relation/component-difference records tied to exact source-span identities.

Consequential or weak relations enter an independent review queue. Human or independent
second-model reviewers can accept, revise, or reject. Resolution preserves rejection evidence,
reranks accepted survivors contiguously, and retains each original matcher candidate identity.
Execution and resolution can be committed to the existing write-once content-addressed archive.

This does not establish scientific novelty. The 52 F8-S4 tests use one synthetic candidate, three
synthetic prior claims generated through the F8-S3 fixture, injected in-memory recall adapters, and
an injected synthetic matcher. There is no live index, production model, real nearest-prior-art
recall measurement, temporal false-novelty result, novelty gate, or research-driver wiring.

## Implemented components

### Frozen multi-channel protocol

`aletheia/knowledge/prior_art_matching.py` adds:

- `RecallChannelManifest` for lexical, embedding, citation, and entity recall, binding code, scorer,
  index snapshot/schema, limits, score/order semantics, model identity where applicable, and zero
  tool authority;
- `PriorArtMatcherManifest`, separating reranker and relation-judgment code/model/parser identities
  and binding the exact strict output schema;
- `PriorArtMatchingProtocol`, bound to an exact `ExtractedAtomicClaimGraphBundle`, graph, corpus,
  ordered candidates, sorted prior pool, all manifests, budgets, thresholds, and failure/review
  policies;
- `build_prior_art_matching_protocol`, which derives candidate targets and prior pool only from the
  reviewed graph.

Runtime recall adapters and matcher manifests must exactly equal the frozen protocol before any
recall request. Every manifest has `tool_policy=none` and an empty tool list.

### Complete recall attempts and union receipts

The executor issues one `PriorArtRecallQuery` for every candidate/channel pair. The query commits
the target, protocol, manifest, prior-pool hash/count, result limit, and time. A strict result
requires canonical score/hash ordering, contiguous ranks, exact exhaustive/truncated semantics,
the same channel/query, the exact pool count, and only known prior claims.

Every attempt is recorded. A channel exception or malformed result is classified with an error
class and derived detail hash; later channels still execute. Any recall failure blocks downstream
matching, but successful partial results remain visible.

`PriorArtRecallCandidate` is the exact unique pair union. It retains per-channel ranks, scores,
result identities, and hit identities. Single-channel hits are never discarded, but cannot become
formal relations. Empty unions and unions with no two-channel pair have distinct failure kinds.

The execution validator rederives the complete union from stored results. An offline caller cannot
remove a weak hit and build a valid smaller ledger.

### No-delete reranking and harness ordering

`PriorArtRerankRequest` names every union candidate in canonical input order. The matcher must return
exactly one bound score for each input in that order. Deletion, addition, reorder, duplicate,
request switching, or pair switching fails closed.

The harness sorts the complete union by score descending, candidate ordinal, and prior hash. It—not
the matcher—assigns global ranks and one of:

- selected for judgment;
- below relation budget;
- insufficient channel support.

All candidates remain in `reranked_candidates`. The execution schema independently replays score
binding, order, ranks, and selection, so a serialized execution cannot forge the harness result.

### Strict scientific relation and component differences

Only selected, multi-channel candidates enter `PriorArtJudgmentBatch`. Every context contains the
structured candidate/prior claims, retrieval signals, rerank identity, and the complete sorted
source-span closure for that prior claim. No source document or tool registry is passed or
persisted.

`PriorArtJudgmentDraft` supports all project relation types:

- equivalent;
- subsumes;
- special case;
- extension;
- combination;
- contradiction.

It separately records relation confidence, difference confidence, semantic-assessment hash, exact
evidence-span hashes, and typed component differences. Equivalent output forbids differences;
every other relation requires at least one. Difference components cover subject, relation, object,
qualifier, population, condition, method, dataset, metric, and effect, with explicit candidate/prior
values and evidence.

The batch must cover every selected pair exactly in harness order. The executor rejects pair or
evidence substitution. It creates `PriorArtRelation` records with exact multi-channel signals,
matcher identity, blocking semantics, differences, and evidence. Execution validation also proves
that relation candidates are the unchanged judgment batch.

### Mechanically derived independent review

Review reasons are derived from the frozen protocol:

- strong-novelty-blocking relation (`equivalent`, `subsumes`, or `special_case`);
- low channel support;
- low relation confidence;
- low difference confidence.

Each `PriorArtReviewTask` binds the relation candidate, claim pair, canonical reasons, and a derived
evidence-package hash. The execution validator recomputes reasons and package identity; removing a
review requirement or substituting a package invalidates the ledger.

`PriorArtRelationReview` supports human or second-model accept/revise/reject. Second-model review
must use a manifest independent of the matcher. Revision is another strict judgment on the same
claim pair and exact evidence closure.

`PriorArtMatchingResolution` partitions every relation. Rejected candidate hashes remain ordered.
Accepted survivors are renumbered contiguously while retaining
`original_relation_candidate_sha256`; reviewed relations gain attributable human/second-model
status and time.

### Stage-aware immutable ledgers

Matching execution and review resolution use `ArchivedKnowledgeLedger`, the generic alias over the
existing write-once archive ledger. Canonical serialization, exclusive create, read-only files,
size/hash verification, and exact object identity apply on commit and load.

A blocked execution retains every completed stage:

- recall/union failure: all recall attempts and any exact partial union;
- rerank failure: complete union and exact rerank request;
- judgment failure: complete rerank request/batch/view and exact judgment request.

No relation is emitted after a failed stage. Raw exception strings and licensed source bytes do not
enter persistent objects. Cancellation propagates instead of being recorded as a scientific
failure.

## Design evidence from related work

The design uses related work to choose audit surfaces, not to claim benchmark performance:

- Citation-informed scientific representation motivates keeping a distinct embedding/citation
  channel rather than treating dense similarity as complete evidence:
  [SPECTER](https://aclanthology.org/2020.acl-main.207/) and
  [SciNCL](https://aclanthology.org/2022.emnlp-main.802/).
- Cross-task scientific representation evaluation motivates frozen model/task identities:
  [SciRepEval/SPECTER2](https://aclanthology.org/2023.emnlp-main.338/).
- Ad-hoc scientific search motivates explicit literature-recall evaluation in the next calibration
  slice: [LitSearch](https://aclanthology.org/2024.emnlp-main.840/).
- Claim co-reference and hierarchy motivate relation judgment at atomic-claim, not document-score,
  level: [SciCo](https://arxiv.org/abs/2104.08809).
- Aspect-sensitive similarity motivates preserving component/facet distinctions:
  [ASPIRE](https://arxiv.org/abs/2111.08366).
- Heterogeneous retrieval results motivate multiple channels and later domain-specific calibration:
  [BEIR](https://arxiv.org/abs/2104.08663/).

F8-S4 does not report a score on any of these datasets.

## Acceptance evidence

The focused suite contains 52 tests across:

| Test file | Tests | Boundary exercised |
|---|---:|---|
| `test_prior_art_matching_protocol.py` | 12 | exact graph/pool/manifests, schema/tool closure, all six relations, canonical recall |
| `test_prior_art_matching_execution.py` | 12 | complete union, singleton/budget retention, order/evidence, failures, determinism, archive, cancellation |
| `test_prior_art_matching_review.py` | 12 | queue closure, human/second-model independence, accept/revise/reject, rerank-after-reject, archive |
| `test_prior_art_matching_adversarial.py` | 16 | manifest drift, invented claims, delete/reorder/switch, evidence/tool attacks, offline ledger forgery |

It verifies:

- exact graph/corpus/candidate/prior-pool binding;
- all four frozen recall channels and zero tool authority;
- all six relation types and component-difference constraints;
- canonical recall and complete union derivation;
- retention but non-promotion of single-channel hits;
- no-delete/no-reorder reranking and harness-controlled ranking/budgets;
- exact judgment pair/evidence closure;
- deterministic execution and write-once execution/resolution loading;
- stage-aware failure ledgers and cancellation propagation;
- blocking/low-confidence review, human and independent second-model paths;
- accept/revise/reject and contiguous survivor ranking;
- manifest drift, invented claims, extra authority, pair/evidence switching, review bypass, retrieval
  signal forgery, union deletion, judgment substitution, and review-package forgery.

Verification on 2026-08-15:

```text
focused F8-S4: 52 passed in 0.66s
complete knowledge suite: 155 passed in 3.41s
full non-Docker suite: 822 passed, 1 skipped, 29 deselected in 296.75s
Docker isolation suite: 29 passed, 823 deselected in 26.57s
```

Exact commands use `conda run -n aletheia` as documented in the guide. The Docker run used the real
local daemon rather than a mocked subprocess boundary.

The changed F8-S4 code/test scope passes Ruff check and format, compilation passes, all 223 public
knowledge exports are present and unique, and `git diff --check` passes. Repository-wide
`ruff check .` still reports the same 20 pre-existing findings in unrelated historical probe
scripts and one old test import; none is in the F8-S4 changed scope.

## Files changed

- `aletheia/knowledge/prior_art_matching.py`;
- `aletheia/knowledge/response_archive.py` (generic knowledge-ledger alias);
- `aletheia/knowledge/__init__.py`;
- `tests/knowledge/f8s4_fixtures.py`;
- the four focused F8-S4 test files above;
- `docs/adr/0013-f8-auditable-multichannel-prior-art-matching.md`;
- `docs/knowledge/PRIOR_ART_MATCHING_AND_REVIEW.md`;
- this report and current README/master-plan/index status updates.

## Explicit non-guarantees

- no production lexical, embedding, citation, or structured-entity index;
- no production recall adapters, reranker, or scientific relation model;
- no measured real-corpus recall, ranking quality, relation accuracy, or difference accuracy;
- no calibrated confidence thresholds or reviewer agreement;
- no author-exclusion, correction/retraction freshness, or ontology/entity-resolution proof beyond
  the frozen input graph;
- no known-answer or temporal false-novelty acceptance (F8-S5);
- no novelty classification, claim-strength, direction, scorecard, SOTA, API, UI, driver, or
  write-up wiring;
- no evidence that any current Aletheia research claim is novel because F8-S4 tests pass.

## Next slice

F8-S5 must turn the existing coverage contracts and F8-S4 relation resolution into a scientifically
calibrated novelty gate. It should freeze known-answer review sets and historical time splits;
measure recall, ranking, relation/difference quality, perturbation stability, and false-novelty;
pre-register thresholds; enforce author exclusion and coverage-unknown ceilings; require
independent novelty review; and only then wire a bounded novelty result into direction selection,
claim strength, scorecards, and write-up.

**Subsequent status (2026-08-15):** F8-S5 has implemented the evaluator-owned calibration,
artifact-derived coverage, author-excluded review, claim ceiling, and explicit direction callback
described above. Its 80-case/240-trial suite is synthetic, so real expert labels, private temporal
custody, production adapters, and a prospective false-novelty result remain scientific gates. See
`F8_S5_CALIBRATED_NOVELTY_IMPLEMENTATION_REPORT_2026_08_15.md` and
`knowledge/CALIBRATED_NOVELTY_ACCEPTANCE.md`.
