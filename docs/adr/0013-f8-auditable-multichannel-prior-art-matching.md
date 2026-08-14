# ADR 0013: Auditable multi-channel prior-art matching with a no-delete reranker

- Status: Accepted
- Date: 2026-08-15
- Scope: F8-S4 / Knowledge Boundary Engine

## Context

F8-S3 produces a reviewed `ExtractedAtomicClaimGraphBundle`: candidate-origin atomic claims,
prior-art atomic claims, and exact source-span evidence edges. It does not say which prior claim is
nearest to a candidate, whether two claims are equivalent or materially different, or how a
retrieval/model ranking was obtained.

That gap is a false-novelty risk. A matcher can look convincing while silently:

- querying only one retrieval representation;
- deleting lexical or citation candidates before an embedding/model reranker is audited;
- allowing a model to choose which candidates survive its own scoring;
- treating a high similarity score as a scientific relation;
- emitting prose differences without component or source-span identities;
- accepting an equivalent/subsuming/special-case match without independent review;
- changing confidence thresholds or model/index identities after seeing results;
- persisting an error string or source document in the scientific ledger.

Related work supports a heterogeneous retrieval design, but does not by itself establish novelty.
SPECTER learns document representations from the citation graph and shows the value of
citation-informed scientific embeddings: [SPECTER](https://aclanthology.org/2020.acl-main.207/).
SciNCL uses citation-graph neighborhoods for contrastive sampling:
[SciNCL](https://aclanthology.org/2022.emnlp-main.802/). SciRepEval/SPECTER2 evaluates scientific
representations across task families rather than assuming one embedding serves every purpose:
[SciRepEval](https://aclanthology.org/2023.emnlp-main.338/). LitSearch makes ad-hoc scientific
literature retrieval a first-class benchmark:
[LitSearch](https://aclanthology.org/2024.emnlp-main.840/). SciCo shows that scientific claim
identity and hierarchy require claim-level reasoning beyond document similarity:
[SciCo](https://arxiv.org/abs/2104.08809). ASPIRE motivates aspect-sensitive scientific similarity:
[ASPIRE](https://arxiv.org/abs/2111.08366). BEIR demonstrates that retrieval behavior varies across
heterogeneous tasks and warns against treating one benchmark or retriever as universal:
[BEIR](https://arxiv.org/abs/2104.08663).

The project therefore needs a reproducible retrieval-and-relation ledger, not an assertion that a
particular embedding, reranker, or synthetic test has solved scientific novelty.

## Decision

### Keep F8-S4 isolated

Add `aletheia/knowledge/prior_art_matching.py` and synthetic tests. The slice consumes a reviewed
`ExtractedAtomicClaimGraphBundle`; it does not modify SURVEY, `ExperimentDriver`, direction
selection, claim strength, novelty acceptance, SOTA comparison, or write-up.

The repository still supplies no production lexical index, vector index, citation graph, entity
index, cross-encoder, or scientific judgment model. All runtime capabilities are injected adapters
whose manifests must equal the frozen protocol.

### Freeze four distinct recall programs

Every protocol contains exactly one manifest, in canonical order, for:

1. lexical recall;
2. embedding recall;
3. citation recall;
4. structured-entity recall.

Each `RecallChannelManifest` binds adapter/scorer code, index snapshot, index schema, result budget,
score range/direction/order, and time. Embedding recall additionally binds a model identity. All
manifests have an empty tool list and `tool_policy=none`.

`PriorArtMatchingProtocol` binds the exact reviewed graph bundle, graph, corpus, ordered candidate
claims, sorted prior-claim pool, four manifests, matcher manifest, relation budget, confidence
thresholds, and review policy. Runtime manifest drift fails before any recall call.

### Record every candidate/channel attempt

For every candidate claim and every channel, the harness issues one typed query bound to the exact
protocol, target, channel manifest, prior-pool identity, pool size, and result limit. It records a
successful typed result or a classified failure; raw exception details become a hash.

Recall results must use canonical score/hash ordering, contiguous ranks, exact truncation semantics,
and only claims from the frozen prior pool. The executor completes all candidate/channel attempts
even after one channel fails. Any recall failure blocks downstream matching, while retaining the
successful partial union for diagnosis.

### Preserve the complete union

Every unique candidate/prior pair returned by any channel becomes one `PriorArtRecallCandidate`.
Its per-channel rank, score, result hash, and hit hash remain attached. Single-channel hits are kept
in the ledger but are ineligible for a formal relation; at least two independent channel receipts
are required.

The execution schema rederives this union from the stored recall results. A caller cannot delete a
weak or inconvenient hit and then construct a valid execution around the smaller set.

### Let the reranker score, never select or delete

The matcher receives the complete recall union in canonical input order and must return exactly one
score for each item in the same order. Deletion, insertion, reordering, pair switching, duplicate
scores, or request switching fails the run.

The harness—not the matcher—sorts by rerank score, candidate ordinal, and prior-claim hash. It then
marks each item as selected, below the relation budget, or insufficiently supported. Every item
remains in `reranked_candidates`, including candidates below the budget. The execution validator
independently replays score binding, order, ranks, and selection.

### Separate retrieval from scientific relation judgment

Only multi-channel candidates selected by the harness enter a strict `PriorArtJudgmentBatch`. For
each pair, the judgment context carries both structured atomic claims, all exact source-span hashes
attached to the prior claim, and all observed retrieval scores. It carries no source document or
tool authority.

The batch must cover the selected pairs exactly and in order. A judgment contains:

- one of `equivalent`, `subsumes`, `special_case`, `extension`, `combination`, or `contradiction`;
- relation confidence;
- exact source-span identities;
- for every non-equivalent relation, one or more typed component differences and a separate
  difference confidence;
- a semantic-assessment hash.

Component differences use subject, relation, object, qualifier, population, condition, method,
dataset, metric, or effect. Each difference names candidate value, prior value, material
distinction, and evidence spans. Equivalent relations cannot invent differences; all other
relations require them.

The harness creates `PriorArtRelation` records with retrieval receipts, matcher identity, exact
evidence, and derived novelty-blocking semantics. It rechecks that relation candidates are the
unchanged judgment batch and that review requirements were derived from the frozen thresholds.

### Require independent review for consequential or weak matches

The review queue is mechanical. It includes a relation when any of these holds:

- the relation is equivalent, subsumes the candidate, or makes it a special case;
- channel support is below the auto-accept threshold;
- relation confidence is below threshold;
- difference confidence is below threshold.

Every task binds the exact relation candidate and a derived evidence-package hash. A human or an
independent second model may accept, revise with another strict judgment on the same pair/evidence
closure, or reject. A second-model manifest cannot equal the matcher manifest.

Resolution partitions every relation. Rejections remain in an ordered ledger. Accepted survivors
are assigned contiguous ranks after rejection while retaining the original relation-candidate hash,
so downstream novelty schemas receive a valid nearest-prior-art ranking without losing the matcher
audit trail.

### Commit execution and resolution separately

Executions and resolutions use the F8 content-addressed archive. Exclusive creation, canonical JSON,
read-only storage, exact size/hash readback, and object-identity checks apply. The execution schema
rederives recall union, reranker order, selected judgment pairs, relation derivation, review reasons,
and review-package identities during load.

The persisted objects contain structured claims and hashes, not licensed source bytes. Raw error
strings are not retained.

## Consequences

- Aletheia now has an isolated, replay-verifiable derivation from a reviewed atomic claim graph to
  ranked relation candidates and component-wise differences.
- Lexical, embedding, citation, and entity evidence remain separately inspectable.
- A model cannot erase union candidates, choose its own final order, or bypass harness budgets.
- Single-channel similarities cannot become formal prior-art relations.
- Strong-novelty-blocking relations and weak judgments cannot silently auto-accept.
- Rejecting a relation does not create rank gaps or erase its original audit identity.
- The 52 synthetic F8-S4 tests prove engineering invariants, not real retrieval recall, relation
  accuracy, or scientific novelty.
- F8-S5 still must calibrate known-answer recall, temporal false-novelty, thresholds, stability, and
  novelty acceptance before any driver integration.

## Rejected alternatives

- **Use only dense retrieval:** loses exact terminology, citation, and structured-entity failure
  channels and overstates one representation's coverage.
- **Take the reranker's top-k output:** lets the model hide deleted candidates and makes missed prior
  art unauditable.
- **Average channel scores before storage:** destroys channel-specific receipts and prevents later
  diagnosis/calibration.
- **Treat similarity as equivalent prior art:** conflates retrieval relevance with a scientific
  relation.
- **Emit one free-form difference paragraph:** cannot prove which scientific component changed or
  which source spans support it.
- **Discard single-channel hits:** hides useful coverage evidence; they remain auditable even though
  they cannot establish a formal relation.
- **Automatically accept a high-confidence equivalent match:** a false blocking relation can kill a
  genuinely novel direction, so blocking semantics require independent review.
- **Keep original ranks after rejection:** violates downstream contiguous-rank contracts; the
  original relation-candidate identity is the correct audit anchor.
- **Wire matching directly into novelty:** synthetic matching invariants are not F8-S5 calibration
  evidence.
