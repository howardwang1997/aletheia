# F8-S3 atomic claim extraction implementation report

- Date: 2026-08-15
- Scope: licensed exact-span extraction, strict structured output, confidence review, immutable
  resolution, graph closure, and derivation replay
- Status: isolated engineering slice complete; real scientific calibration and integration absent

## Outcome

F8-S3 now implements a fail-closed path from an F8-S1 `CorpusIngestionBundle` to exact prior-art
atomic claims and source-span evidence edges. The path separates content access from scientific
interpretation, requires explicit model-input rights, treats literature as untrusted data, persists
no source bytes, and makes every low-confidence accept/revise/reject decision independently
attributable.

The implementation does not make Aletheia a reliable literature reasoner yet. Its 37 new tests use
three synthetic papers, synthetic license evidence, injected in-memory content, and synthetic
extractors. There is no live content resolver, real model call, real extraction accuracy result,
prior-art matcher, novelty gate, or driver wiring.

## Implemented components

### Frozen extraction contract

`aletheia/knowledge/claim_extraction.py` adds:

- `ClaimExtractorManifest` with code/parser/schema/model/instruction identities, supported claim
  types, limits, empty tool authority, and deterministic versus model transport rules;
- `ClaimExtractionProtocol` bound to exact ingestion/corpus/access-policy identities, an exact
  output schema, canonical text normalizer, ordered one-span targets, confidence thresholds, all
  evidence relations, review kinds, and fail-closed target coverage;
- `ClaimExtractionRequest` containing only identities, explicit uses, untrusted-data status, and
  `tool_authority=none`;
- `StructuredClaimDraft`/`StructuredClaimBatch` with `extra=forbid` and an exact JSON-schema hash.

The harness assigns global claim IDs, prior-art origin, paper identity, assertion time, source span,
and initial review state. An extractor cannot author these authority-bearing fields.

### Licensed ephemeral content boundary

`EphemeralSpanContent` is a `repr=False` runtime dataclass, not a persistent knowledge model. The
executor validates:

- paper/grant/span closure;
- `span_extraction` permission for every extractor;
- separate `model_input` permission for model extractors;
- expiry before access;
- canonical document SHA-256 against paper/grant;
- exact span SHA-256 and byte count;
- strict UTF-8, NFKC/whitespace-normalized hash, and locator;
- source verification/rejection state and frozen byte limits.

The resulting `SpanContentReceipt` retains hashes, sizes, uses, and time only. The execution models
have no source-text or evidence-quote field. Error messages are represented by derived hashes.

### Schema-first atomic claims

A structured draft separates:

- subject/relation/object;
- qualifiers, population, and conditions;
- claim type and direction;
- evidence relation;
- claim and evidence confidence;
- quantitative estimate, unit, metric-definition identity, uncertainty/bounds, sample size, and a
  separate quantitative-grounding confidence.

The batch must bind the exact request and span and contain either unique drafts or an explicit
no-claim reason. Unsupported types, missing numeric fields, duplicate claims, wrong binding, extra
authority fields, an entire copied span, or more than the frozen contiguous-source word limit fail
the attempt.

### Complete attempts and review queue

Every ordered target produces one `ClaimExtractionAttempt`. Access, resolution, identity,
extractor, and structured-output errors are classified and retained; later targets still run. Any
failure blocks graph eligibility.

Valid candidates receive mechanically derived reasons for OCR, unverified span, low source
confidence, low claim confidence, low evidence confidence, and low quantitative confidence. The
exact ordered set becomes `ClaimReviewQueue`. High-confidence candidates are automatically
eligible; queued candidates are not.

### Independent accept/revise/reject resolution

`ClaimCandidateReview` binds candidate and evidence-package hashes, reviewer principal/kind,
rationale hash, time, and decision. A second-model reviewer must use a manifest different from the
extractor. Revision uses another strict draft on the same span and produces a new claim identity;
rejection remains in `rejected_candidate_sha256s`.

`ClaimExtractionResolution` requires exactly one review for every queued candidate in queue order,
partitions all candidates, and rederives every accepted reviewed claim/edge. Directly substituting a
different final claim fails validation.

### Contradiction-preserving graph closure

`build_extracted_atomic_claim_graph` combines external candidate-origin claims with accepted
prior-art claims and their exact evidence edges. It preserves `supports`, `refutes`, `qualifies`,
and `mentions`; it does not summarize contradictory evidence away.

`ExtractedAtomicClaimGraphBundle` embeds the resolution and candidate claims and requires its graph
to be the exact ordered view. Removing a reviewed claim or edge is invalid. Existing
`AtomicClaimGraph` validation still requires every prior-art claim to close to source-span evidence.

### Write-once ledgers and honest replay

Extraction execution, review resolution, and graph bundle use the F8 content-addressed ledger
archive. Exclusive creation, read-only files, exact size/hash readback, canonical JSON validation,
and object-identity rechecks apply to all three.

`replay_claim_extraction` does not resample an extractor. It verifies the frozen runtime manifest,
re-resolves and rehashes authorized content, then replays the stored strict output-to-candidate
derivation. Exact evidence is `complete`, unavailable content is `incomplete`, and changed
manifest/content/derivation is `mismatch`.

## Design evidence from related work

The implementation borrows task structure, not benchmark claims:

- SciFact motivates claim labels plus exact evidence rationale rather than document-only verdicts:
  [SciFact](https://aclanthology.org/2020.emnlp-main.609/).
- SciFact-Open motivates treating retrieval coverage and claim verification as distinct problems:
  [SciFact-Open](https://aclanthology.org/2022.findings-emnlp.347/).
- Evidence Inference motivates explicit intervention/comparator/outcome, direction, and full-text
  evidence spans: [Evidence Inference 2.0](https://aclanthology.org/2020.bionlp-1.13/).
- EBM-NLP motivates explicit PICO span fields:
  [EBM-NLP](https://pmc.ncbi.nlm.nih.gov/articles/PMC6174533/).
- SciClaim motivates retaining fine-grained causal/comparative/statistical relations and
  qualifications: [SciClaim](https://arxiv.org/abs/2109.10453/).
- NLI4CT motivates adversarial numerical and multi-evidence checks:
  [NLI4CT](https://aclanthology.org/2023.semeval-1.307/).

These datasets do not establish real F8-S3 accuracy. A separate licensed benchmark and frozen
acceptance thresholds remain F8-S5 work.

## Adversarial verification

The 37 new tests are divided as follows:

| Test file | Tests | Boundary exercised |
|---|---:|---|
| `test_claim_extraction_protocol.py` | 6 | deterministic protocol, exact schema, no tools, target closure, relation preservation, numeric schema |
| `test_claim_extraction_execution.py` | 13 | permissions, expiry, complete attempts, content hashes, malformed output, cancellation, commit/load/replay |
| `test_claim_extraction_review.py` | 10 | queue closure, human/second-model review, accept/revise/reject, immutable resolution/graph, forgery rejection |
| `test_claim_extraction_adversarial.py` | 8 | prompt injection, verbatim copying, wrong binding, duplicates, replay mismatch/unavailability/drift |

Focused result:

```text
37 passed in 0.30s
```

Final regression results:

- complete `tests/knowledge`: **103 passed** in **2.71 s**;
- complete non-Docker project under controlled local PostgreSQL/data-source access:
  **770 passed, 1 skipped, 29 deselected** in **296.81 s**;
- complete real Docker isolation group:
  **29 passed, 771 deselected** in **37.82 s**;
- changed F8-S3 code/test scope: Ruff check and format check pass;
- package/test compilation, 174 unique public exports, and `git diff --check` pass.

The non-Docker total is exactly the F8-S2 baseline plus the 37 F8-S3 tests. Repository-wide
`ruff check .` still reports the same 20 pre-existing findings in unrelated historical probe
scripts and one old test import. Those files were not changed or counted as F8-S3 acceptance.

## Files changed

- `aletheia/knowledge/claim_extraction.py`
- `aletheia/knowledge/response_archive.py`
- `aletheia/knowledge/__init__.py`
- `tests/knowledge/f8s3_fixtures.py`
- `tests/knowledge/test_claim_extraction_protocol.py`
- `tests/knowledge/test_claim_extraction_execution.py`
- `tests/knowledge/test_claim_extraction_review.py`
- `tests/knowledge/test_claim_extraction_adversarial.py`
- `docs/adr/0012-f8-licensed-atomic-claim-extraction-and-independent-review.md`
- `docs/knowledge/CLAIM_EXTRACTION_AND_REVIEW.md`
- this report and status/index updates

No database schema or migration was added. Licensed source bytes remain outside PostgreSQL and the
content-addressed ledger.

## Guarantees delivered

- one immutable attempt per frozen source-span target;
- explicit content-use and expiry enforcement before extraction;
- model input cannot be inferred from span-extraction permission;
- strict output cannot introduce tool authority or unregistered fields;
- raw source bytes and error strings do not persist in F8-S3 ledgers;
- quantitative facts retain unit, metric, uncertainty, and numeric confidence separately;
- OCR/low-confidence work cannot silently become an accepted graph claim;
- review is evidence-bound and second-model review is manifest-independent;
- rejected candidates and contradictory evidence relations remain auditable;
- every accepted prior-art claim closes to an exact source span;
- execution, resolution, and graph are write-once, canonical, and content-addressed;
- replay distinguishes exact derivation, unavailable evidence, and mismatch.

## Explicit non-guarantees

- no real-corpus claim extraction precision, recall, or calibration;
- no production content resolver, PDF/HTML/JATS/OCR parser, or plaintext cleanup proof;
- no production model extractor or OS-level adapter sandbox;
- no semantic entity canonicalization or multi-span claim assembly;
- no nearest-prior-art ranking or component difference (F8-S4);
- no known-answer/temporal false-novelty acceptance (F8-S5);
- no novelty, SOTA, direction, scorecard, API, UI, driver, or write-up wiring;
- no evidence that any scientific claim in the repository is novel because F8-S3 tests pass.

## Next slice

F8-S4 should consume only committed `ExtractedAtomicClaimGraphBundle` artifacts and implement an
auditable nearest-prior-art matcher with lexical, embedding, citation, and structured-entity recall.
Reranking may reorder but never delete candidates or recall-channel receipts. It must output exact
equivalent/subsumes/special-case/extension/combination/contradiction relations and component-wise
differences before F8-S5 can calibrate novelty.

Follow-on status on 2026-08-15: that isolated F8-S4 matcher is now implemented and documented in
`F8_S4_PRIOR_ART_MATCHING_IMPLEMENTATION_REPORT_2026_08_15.md`. The real F8-S5 calibration and
novelty-acceptance gate remain absent.
