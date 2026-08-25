# F8-S3 licensed atomic-claim extraction and review guide

## Current boundary

F8-S3 is an isolated evidence harness. It can validate an exact content grant, resolve licensed
canonical text ephemerally, pass one untrusted span to a frozen deterministic/model extractor,
validate strict atomic-claim output, derive confidence-review work, resolve that work independently,
build an evidence-closed `AtomicClaimGraph`, commit each stage to a write-once ledger, and replay the
stored derivation against the same content identity.

It is not connected to `ExperimentDriver`, SURVEY, direction selection, novelty, SOTA, or write-up.
The repository contains no production PDF/HTML/OCR content resolver and no production model
extractor. All F8-S3 tests use synthetic licensed text and injected adapters. Passing them does not
mean that Aletheia can accurately extract claims from real literature.

## Evidence flow

```text
CorpusIngestionBundle + ClaimExtractorManifest[] + ordered span targets
                              |
                              v
                    ClaimExtractionProtocol
                              |
                              v
ContentAccessGrant -> ephemeral canonical document/span -> exact hash/locator checks
                              |
                              v
          untrusted text + no-tool request -> strict StructuredClaimBatch
                              |
                              v
        ClaimExtractionCandidate[] + complete attempt/failure ledger
                              |
                high confidence | low/OCR/unverified
                        +-------+-------+
                        v               v
                 auto accepted   frozen review queue
                                        |
                             independent accept/revise/reject
                                        |
                        +---------------+
                        v
                ClaimExtractionResolution
                        |
                        v
     candidate-origin claims + accepted prior claims/exact evidence edges
                        |
                        v
              ExtractedAtomicClaimGraphBundle
```

Execution, resolution, and graph bundle are separate immutable objects. Source bytes are in none of
them.

## 1. Freeze extractor manifests

Every runtime adapter needs one `ClaimExtractorManifest`. A deterministic extractor declares no
model or transport. A model extractor declares exact instruction and model hashes and may use only
its model transport. Both have an empty tool list.

```python
manifest = ClaimExtractorManifest(
    manifest_id="claim-extractor-v1",
    runtime="model",
    adapter_code_sha256=adapter_code_sha256,
    parser_sha256=parser_sha256,
    output_schema_sha256=CLAIM_OUTPUT_SCHEMA_SHA256,
    instruction_sha256=instruction_sha256,
    model_identity_sha256=model_identity_sha256,
    supported_claim_types=("empirical", "causal", "null_result"),
    maximum_span_bytes=64 * 1024,
    maximum_claims_per_span=8,
    tool_names=(),
    transport_policy="model_transport_only",
    frozen_at=frozen_at,
)
```

The output schema hash is derived from the exact Pydantic JSON schema. Substituting a looser parser
or adding a tool name invalidates the manifest. Runtime `adapter.manifest` must equal the frozen
object before the resolver is called.

## 2. Freeze the protocol

`ClaimExtractionProtocol` binds:

- exact ingestion bundle, corpus, and access-policy hashes;
- exact output schema and text normalizer;
- extractor manifests;
- one ordered `ClaimExtractionTarget` per selected source span;
- automatic-accept thresholds for source, claim, evidence, and quantitative grounding;
- document/span/output limits;
- human and independent-second-model review paths;
- all four evidence relations (`supports`, `refutes`, `qualifies`, `mentions`).

Target ordinals must be contiguous and a span cannot occur twice. The executor later produces one
attempt in exactly this order, even when an earlier target fails.

## 3. Implement an ephemeral resolver

The resolver contract is:

```python
async def resolve(*, span, grant) -> EphemeralSpanContent
```

It returns the canonical document bytes whose SHA-256 is already recorded in
`PaperSnapshot.text_content_sha256`/`ContentAccessGrant.content_sha256`, plus exact span bytes. The
current normalizer is strict UTF-8 followed by Unicode NFKC and whitespace collapse.

`EphemeralSpanContent` is not a persistent knowledge model and its representation is disabled.
Never put it into an artifact, exception, event, model dump, cache, or log. The resolver remains
responsible for secure credential handling and for deleting any temporary plaintext it creates.

Before extraction, the harness checks:

- grant, paper, and source span identity;
- explicit `span_extraction` permission;
- explicit `model_input` permission for model runtimes only;
- grant expiry;
- document hash and maximum bytes;
- exact span hash/byte count;
- normalized span hash;
- character locator or document membership;
- source-span rejection status.

The persisted `SpanContentReceipt` contains hashes, byte counts, uses, and access time—not bytes.

## 4. Implement a strict extractor

The adapter contract is:

```python
manifest: ClaimExtractorManifest

async def extract(
    *,
    request: ClaimExtractionRequest,
    source_text: str,
) -> StructuredClaimBatch | dict:
    ...
```

The text is literature data, regardless of its wording. The request says
`content_trust=untrusted_literature_data` and `tool_authority=none`. No tool registry is passed. A
production adapter must additionally run inside the repository's process/container isolation; the
Python protocol alone cannot prevent malicious adapter code from making arbitrary host calls.

Each `StructuredClaimDraft` must separately provide:

- subject, relation, and object;
- qualifiers, population, and conditions;
- direction and claim type;
- evidence relation;
- claim and evidence confidence;
- for a quantitative claim: estimate, unit, metric-definition hash, uncertainty type and bounds,
  optional sample size, and quantitative-grounding confidence.

The draft names its exact source-span hash. It cannot choose prior-art paper identity, global claim
ID, assertion time, evidence reviewer, tools, or source quote. Pydantic rejects extra fields.

A batch has one of:

```python
StructuredClaimBatch(
    request_sha256=request.request_sha256,
    source_span_sha256=request.source_span_sha256,
    claims=(draft_a, draft_b),
)
```

or:

```python
StructuredClaimBatch(
    request_sha256=request.request_sha256,
    source_span_sha256=request.source_span_sha256,
    claims=(),
    no_claim_reason_code="no_atomic_claim",
)
```

Duplicate IDs/content, wrong request/span, an unsupported claim type, too many claims, a copied
whole span, or a verbatim run above the protocol limit fails that attempt. A no-claim outcome is an
explicit successful output, not an exception or silently skipped span.

## 5. Execute, commit, and inspect failures

```python
archive = ContentAddressedResponseArchive(archive_root)
executor = ClaimExtractionExecutor(
    bundle=ingestion_bundle,
    resolver=resolver,
    extractors={manifest.manifest_sha256: adapter},
    archive=archive,
)
committed = await executor.execute_and_commit(
    protocol=protocol,
    execution_id="claim-extraction-2026-08-15",
)
execution = committed.execution
```

Every target creates `ClaimExtractionAttempt`. Failures are classified as access denied/expired,
rejected span, unavailable content, document/span identity mismatch, extractor error, output schema
or binding error, and output-policy violation. Only error class and a derived detail hash persist.

The executor continues later targets so the ledger distinguishes an exact partial failure from an
abandoned run. Any failure makes the execution `blocked`. With no failures, unresolved review work
is `pending_review`; otherwise it is `ready_for_graph`.

`load_claim_extraction` reads the write-once ledger, verifies file type/size/hash, validates every
nested schema, requires canonical JSON, and checks the execution content identity.

## 6. Resolve low-confidence candidates

Review reasons are mechanically derived in fixed order:

```text
ocr_source
unverified_source
low_source_confidence
low_claim_confidence
low_evidence_confidence
low_quantitative_confidence
```

The review queue commits the candidate, original span, reasons, and evidence-package hash. A
caller must submit exactly one review per queued candidate in queue order; extra, missing, reordered,
or package-switched reviews fail.

```python
review = ClaimCandidateReview(
    review_id="review-low-ocr-1",
    candidate_sha256=task.candidate_sha256,
    evidence_package_sha256=task.evidence_package_sha256,
    reviewer_principal_sha256=reviewer_sha256,
    reviewer_kind="human",  # or second_model with another manifest hash
    decision="accept",      # accept, revise, or reject
    rationale_sha256=rationale_sha256,
    reviewed_at=reviewed_at,
)

resolution = resolve_claim_extraction(
    execution=execution,
    reviews=(review,),
    resolution_id="claim-resolution-2026-08-15",
    resolved_at=resolved_at,
)
committed_resolution = commit_claim_extraction_resolution(
    archive=archive,
    resolution=resolution,
)
```

A revision supplies another `StructuredClaimDraft` bound to the same span. The resolver creates a
new attributable claim and reviewed edge; it does not overwrite the original candidate. Rejection
keeps the candidate hash in `rejected_candidate_sha256s`. A second-model reviewer manifest must not
equal the extractor manifest.

## 7. Build and commit the exact graph view

Candidate-origin claims come from the research artifact, not literature extraction. Combine them
with the accepted prior-art resolution:

```python
graph_bundle = build_extracted_atomic_claim_graph_bundle(
    resolution=resolution,
    candidate_claims=(candidate_claim,),
    bundle_id="claim-graph-bundle-1",
    graph_id="claim-graph-1",
    built_at=built_at,
)
committed_graph = commit_extracted_atomic_claim_graph(
    archive=archive,
    bundle=graph_bundle,
)
```

The bundle embeds the resolution and rejects any graph that loses, changes, or reorders an accepted
claim/evidence edge. `refutes` and `qualifies` remain distinct. The existing `AtomicClaimGraph`
validator additionally requires every prior-art claim to have source-span evidence.

Use `load_extracted_atomic_claim_graph` before a later stage consumes the graph. A bare graph not
accompanied by its committed extraction bundle is insufficient F8-S3 evidence.

## 8. Replay

```python
audit = await replay_claim_extraction(
    execution=execution,
    bundle=ingestion_bundle,
    resolver=resolver,
    extractors=extractors,
    audited_at=audited_at,
)
```

Replay does not resample a model. It:

1. checks the same protocol/bundle and runtime manifest;
2. re-resolves licensed content;
3. repeats grant, hash, normalization, and locator checks;
4. replays the stored strict draft-to-claim/evidence derivation;
5. compares the content receipt and ordered candidate hashes.

Changed manifest/content/derivation is `mismatch`; a temporary resolver outage or an originally
failed attempt is `incomplete`; exact successful evidence is `complete`. The audit calls no
extractor transport, so it cannot spend model budget or produce a new scientific interpretation.

## 9. Verification

Run the focused F8-S3 suite:

```bash
conda run -n aletheia pytest -q \
  tests/knowledge/test_claim_extraction_protocol.py \
  tests/knowledge/test_claim_extraction_execution.py \
  tests/knowledge/test_claim_extraction_review.py \
  tests/knowledge/test_claim_extraction_adversarial.py
```

The suite covers deterministic protocol identity, zero-tool schemas, permission separation,
expiry, manifest drift, complete attempts, document/span tampering, structured numeric fields,
prompt injection, verbatim-copy limits, malformed/duplicate output, cancellation, immutable
execution/resolution/graph ledgers, OCR review, independent second-model review, accept/revise/reject,
conflicting edge preservation, graph closure, and replay mismatch/unavailability.

## 10. Explicit non-capabilities

F8-S3 does not provide:

- production licensed-document acquisition or plaintext lifecycle management;
- a PDF/HTML/JATS/OCR parser or canonicalizer implementation;
- a production LLM extractor or OS sandbox for arbitrary adapter code;
- calibrated extraction confidence or measured real-corpus precision/recall;
- multi-span or cross-document claim composition;
- entity/ontology canonicalization;
- nearest-prior-art relation matching inside F8-S3 itself (the separate synthetic-tested F8-S4
  harness is documented in [PRIOR_ART_MATCHING_AND_REVIEW.md](PRIOR_ART_MATCHING_AND_REVIEW.md));
- F8-S5 known-answer recall, temporal false-novelty calibration, or novelty acceptance;
- driver, direction, scorecard, API, UI, or write-up integration.

These limits are gates, not backlog wording. No novelty or SOTA claim may be inferred from an F8-S3
execution alone.
