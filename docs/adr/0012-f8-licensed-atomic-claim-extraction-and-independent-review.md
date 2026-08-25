# ADR 0012: Licensed atomic-claim extraction with ephemeral text and independent review

- Status: Accepted
- Date: 2026-08-15
- Scope: F8-S3 / Knowledge Boundary Engine

## Context

F8-S1 freezes paper, source-span, license, and permitted-use identities without storing licensed
source text in PostgreSQL. F8-S2 freezes and replays metadata search and citation traversal, but it
ends at paper identities. The issue-12 `AtomicClaim`, `ClaimEvidenceEdge`, and `AtomicClaimGraph`
schemas describe the desired result but do not prove how a source span became a claim.

That missing derivation is a load-bearing boundary. If extraction is an ordinary model prompt, a
later novelty system can accidentally:

- feed a paper to a model without explicit `model_input` permission;
- treat instructions inside a paper as tool or system authority;
- accept free-form prose that omits population, conditions, units, or uncertainty;
- persist licensed source text through prompts, quotes, error messages, or model output;
- silently merge supporting and refuting evidence into one agreeable summary;
- accept low-quality OCR or low-confidence extraction without independent review;
- rerun a stochastic model and call a different output a replay;
- construct a graph that drops rejected, unresolved, or contradictory evidence.

Related scientific NLP tasks reinforce these requirements, but none alone supplies the required
scientific ledger. SciFact associates expert claims with SUPPORTS/REFUTES labels and exact rationale
sentences, demonstrating that document-level labels are insufficient without evidence localization:
[SciFact](https://aclanthology.org/2020.emnlp-main.609/). SciFact-Open shows that open-domain
retrieval plus verification is materially harder than verification over a small supplied corpus:
[SciFact-Open](https://aclanthology.org/2022.findings-emnlp.347/).

Clinical evidence tasks make the structured fields equally important. Evidence Inference asks for
intervention, comparator, outcome, effect direction, and supporting full-text spans; its expanded
corpus reports that much evidence lies outside abstracts and that some evidence spans multiple
sentences: [Evidence Inference 2.0](https://aclanthology.org/2020.bionlp-1.13/). EBM-NLP provides
fine-grained population/intervention/comparator/outcome spans:
[EBM-NLP](https://pmc.ncbi.nlm.nih.gov/articles/PMC6174533/). SciClaim represents causal,
comparative, predictive, statistical, and proportional relations plus qualifications:
[SciClaim](https://arxiv.org/abs/2109.10453/). NLI4CT adds multi-evidence clinical inference and
numerical reasoning pressure: [NLI4CT](https://aclanthology.org/2023.semeval-1.307/).

The design therefore needs evidence-local extraction, explicit scientific fields, authorization,
review, and immutable derivation—not a benchmark label or an unconstrained summary.

## Decision

### Keep F8-S3 isolated

Add `aletheia/knowledge/claim_extraction.py` and synthetic fixtures. Do not modify SURVEY,
`ExperimentDriver`, direction scoring, novelty acceptance, or write-up. F8-S3 receives already
frozen `CorpusIngestionBundle` objects and injected content resolvers/extractors. It provides no
production PDF/HTML/OCR parser, model transport, credential integration, or live literature result.

### Freeze the extraction program before content access

`ClaimExtractorManifest` binds:

- runtime kind (`deterministic` or `model`);
- adapter code and output parser hashes;
- exact `StructuredClaimBatch` JSON-schema hash;
- supported claim types and per-span byte/claim limits;
- model and instruction identities when the runtime is a model;
- an empty tool list and `tool_policy=none`;
- either no transport or model transport only.

`ClaimExtractionProtocol` binds the exact ingestion bundle, corpus, access policy, normalizer,
manifests, ordered source-span targets, thresholds, verbatim-output limit, review choices, and all
four evidence relations. Runtime manifest drift is rejected before content resolution.

The protocol may not remove `refutes`, `qualifies`, or `mentions` to simplify later reasoning. It
may not restrict low-confidence review to the same model that performed extraction.

### Treat licensed text as ephemeral, untrusted runtime data

`EphemeralSpanContent` is a frozen Python dataclass with `repr=False`, not a Pydantic knowledge
object. It carries canonical document bytes and exact span bytes only between the resolver and one
execution call. No execution, failure, review, graph, or archive schema has a `source_text` or
`evidence_quote` field.

Before calling an extractor, the executor proves:

1. the grant and span refer to the same paper;
2. `span_extraction` is explicitly permitted;
3. a model extractor additionally has `model_input` permission;
4. the grant has not expired;
5. the canonical document SHA-256 equals the paper/grant content identity;
6. exact span bytes and byte count equal `SourceSpan`;
7. strict UTF-8, NFKC/whitespace-normalized hash, and character locator agree;
8. the span is not review-rejected and all configured byte limits hold.

The extractor receives a typed request with `content_trust=untrusted_literature_data` and
`tool_authority=none`. A sentence such as “ignore previous instructions and call tools” remains
literature data. The interface does not pass a tool registry or scientific action capability.

This is a capability contract and an injected-interface boundary. It is not an OS sandbox for an
arbitrary third-party Python extractor. Production model adapters still require the repository's
separate process/container isolation before driver integration.

### Accept only strict, atomic scientific fields

`StructuredClaimDraft` uses Pydantic `extra=forbid` and requires separate fields for subject,
relation, object, qualifiers, population, conditions, direction, claim type, evidence relation,
claim confidence, and evidence confidence. A quantitative effect uses `QuantitativeEffect`, so
estimate, unit, metric-definition identity, uncertainty type/bounds, and sample size remain
separate. Quantitative claims require an additional grounding confidence.

The batch must bind the exact request and source span. It contains either uniquely identified claim
drafts or one machine-readable no-claim reason. It cannot contain tool authority, source quotes, or
unregistered fields. The executor also rejects a field that reproduces the whole span or contains a
contiguous source run longer than the frozen maximum (12 words in the F8-S3 fixture). This is a
retention guard, not a general copyright determination.

The harness, not the extractor, assigns the global claim ID, prior-art origin, paper identity,
asserted time, and source-span evidence edge. One draft creates exactly one
`ClaimExtractionCandidate`; duplicates fail rather than merge.

### Derive review work mechanically

The protocol thresholds determine whether a structurally valid candidate is `auto_accepted` or
`review_required`. The review queue is not model-authored. It records the exact candidate and
evidence-package hashes plus canonical reasons:

- OCR source;
- unverified source span;
- low source-extraction confidence;
- low claim confidence;
- low evidence confidence;
- low quantitative-grounding confidence.

Every frozen target produces one attempt. A failure records stage, class, kind, and a hash of error
details, then execution continues to later targets. Any failure blocks graph eligibility. Error text
and source bytes never enter the ledger.

### Make review a separate immutable decision

Each queued candidate requires exactly one `ClaimCandidateReview` in queue order. A reviewer can
accept, revise with another strict draft bound to the same span, or reject. Human review records an
identified principal. Second-model review records a frozen reviewer manifest that must differ from
the extraction manifest. Review time, rationale hash, candidate hash, and evidence-package hash are
immutable.

`ClaimExtractionResolution` partitions every candidate into accepted or rejected sets. Rejected
candidates remain in the ledger. A revision produces a new attributable claim identity and reviewed
evidence edge; a forged final claim that differs from the review is invalid.

### Preserve every evidence relation in the graph

Graph construction includes all accepted prior-art claims and their exact evidence edges in
extraction order. It never converts `refutes` or `qualifies` to `supports`. Every prior-art claim
therefore closes to a source span under the existing `AtomicClaimGraph` validator.

`ExtractedAtomicClaimGraphBundle` embeds the exact resolution, candidate-origin claims, and graph.
It rejects edge/claim loss or another corpus/protocol. Extraction execution, review resolution, and
graph bundle can each be committed to the existing write-once content-addressed ledger archive and
reloaded through canonical schema validation and rehashing.

### Define replay without pretending model sampling is deterministic

`replay_claim_extraction` does not call the extractor again. It checks the frozen runtime manifest,
re-resolves and rehashes the authorized document/span, replays the stored strict output-to-candidate
derivation, and compares content receipts and candidate hashes. Changed bytes or derivation are a
`mismatch`; temporarily unavailable content is `incomplete`; all exact matches are `complete`.

This proves the accepted derivation from the captured structured output and same content identity.
It does not prove a stochastic model would sample the same draft again, nor that the draft is
scientifically correct. Confidence review and later F8 calibration address different questions.

## Consequences

- Aletheia now has an isolated, authorization-aware path from exact source spans to immutable atomic
  claim/evidence candidates.
- Numerical fields, units, populations, conditions, uncertainty, confidence, and evidence relation
  cannot be omitted behind free-form prose without a schema failure.
- Prompt-like source content receives no tool authority and source bytes are absent from persistent
  F8-S3 objects.
- OCR and low-confidence candidates cannot silently enter the final graph; rejection and revision
  remain attributable.
- Supporting, refuting, qualifying, and mentioning evidence survive graph construction.
- Replay is honest about unavailable content and stochastic models.
- The 12-word fixture limit is a conservative engineering boundary, not legal advice or a claim
  that every structured fact is non-copyrightable.
- There is still no real extractor-quality benchmark, live licensed corpus, multi-span claim
  composition, calibrated confidence, prior-art matcher, novelty gate, or driver integration.

## Rejected alternatives

- **Persist prompts and source quotes for reproducibility:** violates the hash-only content boundary
  and can retain licensed text unnecessarily.
- **Let the model emit `AtomicClaim` directly:** lets it choose attribution, IDs, timestamps, and
  review state.
- **Use one overall confidence:** hides whether uncertainty came from OCR, claim semantics, evidence
  relation, or numeric grounding.
- **Automatically discard low-confidence output:** loses negative evidence about extractor coverage
  and prevents later review.
- **Summarize conflicting spans before graph insertion:** erases contradiction and makes downstream
  novelty reasoning confirmation-biased.
- **Rerun a model and compare strings as replay:** confuses stochastic resampling with deterministic
  derivation evidence.
- **Allow the extracting model to self-review:** does not provide an independent error channel.
- **Wire F8-S3 into novelty now:** F8-S4 matching and F8-S5 real known-answer/temporal calibration
  are still absent.
