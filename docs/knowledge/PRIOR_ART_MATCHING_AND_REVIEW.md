# F8-S4 auditable prior-art matching and review guide

## Current boundary

F8-S4 consumes an F8-S3 `ExtractedAtomicClaimGraphBundle` and can run four frozen recall channels,
retain their complete union, score every union candidate, derive a deterministic final order,
produce strict scientific relation/component-difference candidates, route consequential or weak
matches to independent review, and commit execution/resolution ledgers.

It is an isolated evidence harness. The repository has no production literature indexes or matcher,
and F8-S4 is not connected to SURVEY, `ExperimentDriver`, direction selection, novelty, SOTA,
scorecards, or write-up. All tests use three synthetic prior claims. Passing them does not show that
Aletheia can retrieve nearest real prior art or judge novelty accurately.

## Evidence flow

```text
reviewed ExtractedAtomicClaimGraphBundle
              |
              v
 four RecallChannelManifest objects + PriorArtMatcherManifest
              |
              v
      PriorArtMatchingProtocol
              |
              v
 candidate x {lexical, embedding, citation, entity}
              |
       complete attempts/results/failures
              |
              v
 unique pair union with all channel receipts
       |                         |
       | one channel             | at least two channels
       v                         v
 audit only             complete-union rerank request
                                  |
                         score every item, no filtering
                                  |
                       harness sort + selection budget
                                  |
                    strict relation/difference judgment
                                  |
                  auto accept or frozen review queue
                                  |
                   independent accept/revise/reject
                                  |
                                  v
                    PriorArtMatchingResolution
```

Execution and resolution are separate immutable objects. The execution retains candidates below
the formal relation threshold and below the selection budget.

## 1. Start from the reviewed graph bundle

Use the exact F8-S3 graph bundle, not a loose `AtomicClaimGraph`:

```python
graph_bundle = load_extracted_atomic_claim_graph(
    archive=archive,
    ledger=graph_ledger,
)
```

The bundle proves how every prior claim was extracted, reviewed, and linked to exact source spans.
The matching protocol binds all of these identities:

- graph-bundle hash;
- graph hash;
- corpus-snapshot hash;
- ordered candidate-origin claims;
- unique sorted prior-art claim pool.

Every prior claim must have at least one evidence edge. Protocol construction fails if the graph is
not the exact reviewed view.

## 2. Freeze the four recall manifests

Create one manifest for every `PriorArtRecallChannel`, in enum order:

```python
lexical = RecallChannelManifest(
    manifest_id="lexical-recall-v1",
    channel="lexical",
    adapter_code_sha256=adapter_code_sha256,
    scorer_sha256=scorer_sha256,
    index_snapshot_sha256=index_snapshot_sha256,
    index_schema_sha256=index_schema_sha256,
    maximum_results_per_claim=100,
    tool_names=(),
    frozen_at=frozen_at,
)
```

Embedding recall additionally requires `model_identity_sha256`. Each manifest fixes the score
range, direction, canonical result order, result limit, index, code, and zero-tool policy. The four
channels are:

- lexical;
- embedding;
- citation;
- structured entity.

The harness does not prescribe BM25, a vector model, a citation service, or an ontology. Those are
adapter choices whose exact identities belong in the manifest and later F8-S5 calibration.

## 3. Freeze the matcher manifest and protocol

`PriorArtMatcherManifest` separately identifies reranking and relation judgment:

```python
matcher_manifest = PriorArtMatcherManifest(
    manifest_id="prior-art-matcher-v1",
    reranker_code_sha256=reranker_code_sha256,
    reranker_model_sha256=reranker_model_sha256,
    reranker_parser_sha256=reranker_parser_sha256,
    judgment_code_sha256=judgment_code_sha256,
    judgment_model_sha256=judgment_model_sha256,
    judgment_instruction_sha256=judgment_instruction_sha256,
    judgment_parser_sha256=judgment_parser_sha256,
    judgment_schema_sha256=PRIOR_ART_JUDGMENT_SCHEMA_SHA256,
    tool_names=(),
    frozen_at=frozen_at,
)
```

It must support all six relation types and all ten difference components. Its rerank output policy
is `score_every_union_candidate_no_filter`; its judgment output policy is
`strict_selected_pair_batch`.

Build the protocol:

```python
protocol = build_prior_art_matching_protocol(
    protocol_id="prior-art-protocol-v1",
    graph_bundle=graph_bundle,
    recall_manifests=(lexical, embedding, citation, entity),
    matcher_manifest=matcher_manifest,
    maximum_relations=20,
    minimum_auto_accept_channels=3,
    minimum_auto_relation_confidence=0.90,
    minimum_auto_difference_confidence=0.90,
    frozen_at=frozen_at,
)
```

Two channels are always the minimum for a formal relation. The auto-accept threshold may be stricter.
`maximum_relations` limits expensive relation judgment; it does not remove candidates from the
rerank audit.

## 4. Implement recall adapters

Each adapter exposes its exact manifest and one async method:

```python
manifest: RecallChannelManifest

async def retrieve(
    *,
    query: PriorArtRecallQuery,
    candidate_claim: AtomicClaim,
    prior_claims: tuple[AtomicClaim, ...],
) -> PriorArtRecallResult | dict:
    ...
```

The query fixes the candidate, prior-pool hash/count, channel, manifest, result limit, and issue
time. A result must:

- bind `query.query_sha256` and the same channel;
- report the exact examined pool count;
- contain only prior hashes from that pool;
- rank hits contiguously;
- order hits by score descending then prior hash ascending;
- distinguish exhaustive from truncated output and bind an exact cutoff when truncated.

The adapter gets structured claims, not source documents or tools. A production adapter is still
responsible for credentials, network isolation, rate limits, and secure index construction.

The executor makes one attempt for every candidate/channel pair. A channel error is classified and
hashed; remaining channels still run. Any error blocks reranking, because silently continuing with a
weaker recall program would change the frozen scientific protocol.

## 5. Understand union semantics

The executor mechanically groups hits by candidate/prior pair. Each union item retains, per channel:

- channel name;
- rank;
- score;
- result hash;
- hit hash.

One-channel candidates receive `insufficient_channel_support`. They stay in the execution and later
rerank audit if another eligible candidate exists, but cannot enter relation judgment. If the union
is empty, execution fails with `empty_recall_union`. If no pair has at least two channels, it fails
with `insufficient_channel_agreement`.

During construction or load, `PriorArtMatchingExecution` rederives the union from all successful
results. Deleting a low-score or inconvenient hit invalidates the object.

## 6. Implement the no-delete matcher

The injected matcher has one manifest and two methods:

```python
async def rerank(
    *,
    request: PriorArtRerankRequest,
    candidates: tuple[PriorArtRecallCandidate, ...],
    claims: Mapping[str, AtomicClaim],
) -> PriorArtRerankBatch | dict:
    ...

async def judge(
    *,
    request: PriorArtJudgmentRequest,
    contexts: tuple[PriorArtJudgmentContext, ...],
) -> PriorArtJudgmentBatch | dict:
    ...
```

`rerank` must return one `PriorArtRerankScore` per input candidate in input order. It cannot return a
top-k list. The harness validates the candidate hash and pair, then orders the complete union by:

1. rerank score descending;
2. candidate ordinal;
3. prior-claim SHA-256 ascending.

The harness assigns global ranks and selections. Eligible items beyond `maximum_relations` become
`below_relation_budget`; one-channel items become `insufficient_channel_support`. Neither is lost.

## 7. Return strict relations and component differences

`judge` receives only the harness-selected pairs, in order. Every context includes both atomic
claims, exact prior evidence-span hashes, rerank identity, and multi-channel retrieval signals.

Each `PriorArtJudgmentDraft` must choose one relation:

- `equivalent`;
- `subsumes`;
- `special_case`;
- `extension`;
- `combination`;
- `contradiction`.

Equivalent means no component difference and requires `difference_confidence=None`. Every other
relation requires at least one unique `ComponentDifference` and a separate confidence. Components
are subject, relation, object, qualifier, population, condition, method, dataset, metric, or effect.
Difference evidence must be a subset of the judgment's exact source-span closure.

The batch must preserve every selected pair in order. Pair deletion, insertion, reorder, request
switch, extra authority, or evidence substitution blocks execution.

## 8. Execute and commit

```python
executor = PriorArtMatchingExecutor(
    graph_bundle=graph_bundle,
    recall_adapters={
        manifest.manifest_sha256: adapter
        for manifest, adapter in recall_adapter_pairs
    },
    matcher=matcher,
    archive=archive,
)

committed = await executor.execute_and_commit(
    protocol=protocol,
    execution_id="prior-art-match-2026-08-15",
)
execution = committed.execution
```

Execution dispositions are:

- `blocked`: recall, union, rerank, or judgment failed;
- `pending_review`: all computation closed, but one or more relations require review;
- `ready`: all relation candidates are auto-accepted.

For failures, completed earlier stages remain inspectable. A reranker failure retains the complete
recall union and exact rerank request. A judgment failure also retains the completed rerank batch,
deterministic reranked view, and exact judgment request.

Reload with:

```python
execution = load_prior_art_matching(archive=archive, ledger=execution_ledger)
```

Loading rechecks canonical JSON, archive identity, recall derivation, complete-union scoring,
harness ordering, judgment binding, relation derivation, review thresholds, and evidence-package
hashes.

## 9. Resolve review work

Review is required when a relation blocks strong novelty or has low channel, relation, or difference
confidence. The task includes canonical reasons and a derived evidence-package hash.

Create one `PriorArtRelationReview` per task, in queue order. Decisions are:

- `accept`;
- `revise`, with a new strict judgment on the same pair and exact evidence closure;
- `reject`.

A human review records a principal. A second-model review also records a manifest that must differ
from the original matcher manifest. Review cannot change the pair or evidence closure.

```python
resolution = resolve_prior_art_matching(
    execution=execution,
    reviews=tuple(reviews),
    resolution_id="prior-art-resolution-1",
    resolved_at=resolved_at,
)
committed_resolution = commit_prior_art_matching_resolution(
    archive=archive,
    resolution=resolution,
)
```

Rejected relation-candidate hashes remain ordered in the resolution. Accepted survivors are ranked
again from 1 without gaps. Each survivor retains `original_relation_candidate_sha256`, so a rank
change after rejection does not erase the original matcher output.

## 10. Verification

Run the focused F8-S4 suite:

```bash
conda run -n aletheia pytest -q \
  tests/knowledge/test_prior_art_matching_protocol.py \
  tests/knowledge/test_prior_art_matching_execution.py \
  tests/knowledge/test_prior_art_matching_review.py \
  tests/knowledge/test_prior_art_matching_adversarial.py
```

The 52 tests cover exact graph/pool binding, four manifests, zero-tool boundaries, all six relations,
ten-component schema closure, canonical recall, complete union derivation, singleton retention,
deterministic no-delete reranking, selection budgets, exact source-span evidence, stage-aware
failures, cancellation, immutable ledgers, accept/revise/reject, independent second-model review,
post-rejection reranking, manifest drift, pair/evidence substitution, review bypass, and offline
ledger forgery.

Run all knowledge tests with:

```bash
conda run -n aletheia pytest -q tests/knowledge
```

## 11. Explicit non-capabilities

F8-S4 does not provide:

- production lexical/vector/citation/entity indexes or adapters;
- a production reranker or scientific relation model;
- measured known-answer nearest-prior-art recall;
- calibrated relation/difference accuracy or confidence;
- entity/ontology resolution beyond injected structured claim fields;
- author exclusion, retraction/correction freshness, or live temporal refresh beyond frozen inputs;
- F8-S5 temporal false-novelty evaluation and novelty acceptance;
- driver, direction, claim-strength, SOTA, API, UI, or write-up integration;
- evidence that any candidate claim in the repository is scientifically novel.

These are release gates. A valid F8-S4 resolution is necessary evidence for novelty analysis, not a
novelty verdict.

The subsequent F8-S5 engineering harness now consumes this resolution for synthetic-tested
calibration, artifact-derived coverage, independent novelty review, and direction gating; see
[CALIBRATED_NOVELTY_ACCEPTANCE.md](CALIBRATED_NOVELTY_ACCEPTANCE.md). It does not change the lack of
real retrieval/relation calibration stated above.
