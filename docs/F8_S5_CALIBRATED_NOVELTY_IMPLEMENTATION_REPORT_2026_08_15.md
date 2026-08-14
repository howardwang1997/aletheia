# F8-S5 calibrated novelty acceptance implementation report

- Date: 2026-08-15
- Scope: evaluator-owned calibration, artifact-derived live coverage, independent novelty review,
  claim ceiling, and research-direction gating
- Engineering status: complete
- Scientific-exit status: not complete

## Outcome

F8-S5 now supplies a replay-verifiable engineering path from an evaluator-owned known-answer and
strictly later temporal-holdout suite to a bounded research-direction decision. It closes the main
software loopholes left after F8-S4:

- every case and semantics-preserving perturbation must produce one signed, non-reused receipt;
- calibration uses one-sided confidence bounds on both validation and temporal holdout;
- false strong novelty is measured separately from general classification accuracy;
- live coverage values are derived from exact F8-S1 through F8-S4 and correction artifacts;
- failed global calibration and fewer than three prior relations block a live direction;
- candidate authors cannot review the novelty package;
- confirmed domain-expert and research-librarian roles are both required;
- relation evidence mechanically determines classification and exact differences;
- coverage, review state, and class mechanically determine the claim ceiling;
- discovery can consume an exact atomic-claim-bound direction gate instead of the legacy shortcut.

This is an engineering result. The included 80 cases, 240 trial receipts, literature records,
reviewers, and matcher outputs are synthetic fixtures. No real candidate in the repository has
thereby passed a scientific novelty audit.

## Research and protocol basis

The implementation review considered current primary work on research-idea novelty and scientific
retrieval:

- [RINoBench](https://arxiv.org/abs/2603.10303) — expert-labelled research-idea novelty and the gap
  between resemblance rationales and reliable scores;
- [Axiomatic Benchmark](https://arxiv.org/abs/2604.15145) — no examined novelty metric satisfies
  every desired axiom;
- [RQ-Bench](https://arxiv.org/abs/2606.12071) — limits of LLM research-quality judging relative to
  experts;
- [ScholarEval](https://arxiv.org/abs/2510.16234) — literature-grounded evaluation of soundness and
  contribution;
- [NovBench](https://arxiv.org/abs/2604.11543) — multi-dimensional novelty-review evaluation;
- [Idea Novelty Checker](https://aclanthology.org/2025.sdp-1.9/) — retrieval-based comparison of
  scientific ideas;
- [TREC Total Recall](https://trec.nist.gov/data/total-recall/2016/Total%20Recall%20Guidelines%202016.html)
  — high-recall review protocol precedent;
- [LitSearch](https://aclanthology.org/2024.emnlp-main.840/) — ad-hoc scientific literature
  retrieval evaluation.

The resulting decision is to measure retrieval, temporal errors, perturbation stability, relation
classification, and independent review separately rather than compressing novelty into one model
score. ADR 0014 records the full decision and rejected alternatives.

## 1. Evaluator-owned calibration

`aletheia/knowledge/novelty_calibration.py` adds:

- `NoveltyCalibrationPolicy` with fixed minimum sample composition and threshold directions;
- strictly ordered validation and later temporal-holdout cases;
- base, claim-paraphrase, query-synonym, entity-alias, and condition-reorder variant types;
- evaluator-only label commitments and author-excluded expert adjudication identities;
- a frozen evaluator manifest with no tool authority and exact classifier hash;
- successful and explicit-error trial payloads;
- HMAC-SHA-256 receipts using at least 32-byte keys;
- one-sided 95% Wilson rates and exact MRR derivation;
- per-split metrics, failure reasons, and a mechanical PASS/FAIL report;
- write-once report commit/load with canonical JSON, object identity, and signature revalidation.

The default minimum suite has 40 cases per split. Each split must include at least 30 known
non-strong and 10 strong-novel labels; each case must have at least three semantics-preserving
variants. Every validation cutoff precedes every temporal-holdout cutoff.

Every sealed case/variant appears exactly once and in order. Trial, receipt, resolution, and search
identities cannot be reused. Failed trials preserve the exception class and a message hash, never
the raw message, and always generate a `trial_error` failure.

The report separately derives:

- known-answer recall;
- seed-reference recovery;
- base classification accuracy;
- false strong-novelty rate on known non-strong cases;
- missed strong-novelty rate;
- perturbation stability against the base result and ranked prior sequence;
- nearest-prior MRR;
- failed trial count.

Minimum signals use the Wilson lower bound; error signals use the Wilson upper bound. Both splits
must independently pass. In the perfect 40-case synthetic split, for example, the recall lower
bound is approximately 0.937, the zero-of-30 false-strong upper bound approximately 0.083, and the
zero-of-10 missed-strong upper bound approximately 0.213. The point estimate alone never controls
acceptance.

## 2. Frozen relation-to-class rule

`NOVELTY_CLASSIFICATION_POLICY_SHA256` binds one deterministic classifier used in both calibration
and live decisions:

1. any `equivalent` relation blocks as `known_equivalent`;
2. any `subsumes` or `special_case` relation blocks as `known_special_case`;
3. otherwise the top relation selects contradiction or combination;
4. otherwise exact method differences yield `novel_method`;
5. relation/object/effect differences yield `novel_phenomenon`;
6. remaining differences yield `incremental_extension`.

`classify_prior_art_relations` accepts only non-empty, contiguous reviewed ranks. Calibration and
live paths cannot substitute a different prose judgment after the relation ledger is frozen.

## 3. Artifact-derived live coverage

`aletheia/knowledge/novelty_coverage.py` adds a calibrated live policy and a self-contained
`CalibratedNoveltyCoverageAssessment`. Its public builder intentionally has no external-observation
argument. It derives six formerly caller-provided signals:

| Signal | Derivation |
|---|---|
| known-answer recall | temporal-holdout one-sided lower bound |
| seed-reference recovery | exact frozen seeds present in replayed campaign hits |
| full-text availability | full-text grants for distinct resolved prior papers |
| source-span verification | resolved relation spans present in the ingested corpus |
| correction/retraction check | complete bound report covering every resolved prior paper |
| perturbation stability | temporal-holdout one-sided lower bound |

F8-S2 derives query-family completeness, source diversity, citation saturation, and uncovered
source fraction as before. F8-S5 rebuilds the exact policy from the calibration thresholds and
requires it to freeze before live search begins.

The combined verdict fails when:

- global calibration is not `PASS`;
- any of the ten hard coverage signals fails;
- any candidate claim has fewer than the policy's three accepted nearest-prior relations.

Corpus, cutoff, ingestion bundle, reviewed graph, matching resolution, search campaign, correction
report, grants, papers, and source spans are exact-bound. Commit/load revalidates all derivations and
calibration signatures.

## 4. Independent review and claim ceilings

`aletheia/knowledge/novelty_decision.py` adds:

- a candidate-authorship manifest tied to one graph and candidate artifact;
- an evidence package with exact calibrated coverage, relation hashes, derived class, exact
  differences, temporal/model-prior/contamination disclosures, and blockers;
- immutable novelty reviews with reviewer credential and attestation hashes;
- a reviewed novelty decision that rederives the legacy `NoveltyAssessment` contract;
- a mechanical research-direction gate and write-once archive.

Candidate authors cannot review. Reviews are canonical, unique, package-bound, post-assembly, and
role-typed. Authorization requires confirmed `domain_expert` and `research_librarian` roles; two
domain experts do not substitute for retrieval expertise. A search request or classification
rejection remains an explicit blocker.

Mechanical outcomes are:

- insufficient calibration/coverage: indeterminate, speculative, blocked;
- equivalent/special-case prior: no novelty claim, rejected direction;
- unresolved review/search: research more, no experiment authorization;
- confirmed incremental or contradictory direction: authorized but weak ceiling;
- confirmed strong class with every prerequisite: authorized with moderate ceiling.

The gate never emits an unbounded or stronger-than-moderate novelty claim.

## 5. Discovery integration

`aletheia/research/discovery.py` now accepts `auditable_novelty_gate_fn`. When supplied, the legacy
paper-count and critic-panel shortcut is not used. The callback must return a real
`ResearchDirectionGate`, and its one candidate claim must equal the candidate dictionary's
`candidate_claim_sha256`. Wrong type, callback exception, identity mismatch, coverage failure,
known classification, and any non-authorized disposition fail closed.

The legacy path remains the default for backward compatibility. Therefore the repository does not
yet claim that the default scheduler automatically materializes F8-S1 through F8-S5 for every
generated idea. Migrating scheduler, scorecard, write-up, API, and UI consumers remains integration
work.

## Test evidence

Focused F8-S5 acceptance:

```text
36 calibration protocol/report tests
 9 artifact-derived live coverage and F8-S4 integration tests
12 novelty review, claim-ceiling, and direction-gate tests
 2 discovery exact-gate integration tests
59 passed in 4.77s
```

The focused tests cover temporal overlap, weak policy floors, hidden-label substitution, author
self-review, classifier drift, short keys, missing/reordered/reused trials, invalid signatures,
forged metrics, validation-pass/holdout-fail behavior, perturbation instability, archived wrong
keys, caller-substituted coverage, global calibration failure, incomplete correction checks,
three-prior floors, cross-corpus artifacts, real F8-S4-to-calibration receipt issuance, role
deficiency, review rebinding, class upgrade, gate-bit forgery, known/incremental/strong outcomes, and
candidate/gate identity mismatch in discovery.

Acceptance runs:

```text
complete knowledge suite: 214 passed in 8.11s
full non-Docker suite: 881 passed, 1 skipped, 29 deselected in 303.46s
real Docker isolation suite: 29 passed, 882 deselected in 37.80s
```

The changed Python scope passes Ruff check and compilation. All new Python files pass Ruff format;
the edited historical discovery module retains its pre-existing formatting style. All 271 public
knowledge exports are present and unique. `git diff --check` passes.

## Files added or materially changed

- `aletheia/knowledge/novelty_calibration.py`;
- `aletheia/knowledge/novelty_coverage.py`;
- `aletheia/knowledge/novelty_decision.py`;
- `aletheia/knowledge/__init__.py`;
- `aletheia/research/discovery.py`;
- `tests/knowledge/f8s5_fixtures.py`;
- `tests/knowledge/test_novelty_calibration_protocol.py`;
- `tests/knowledge/test_novelty_calibration_report.py`;
- `tests/knowledge/test_novelty_coverage.py`;
- `tests/knowledge/test_novelty_decision.py`;
- `tests/knowledge/test_discovery_direction_gate.py`;
- `docs/adr/0014-f8-calibrated-novelty-acceptance-and-direction-gate.md`;
- `docs/knowledge/CALIBRATED_NOVELTY_ACCEPTANCE.md`;
- this report and README/master-plan/docs-index status updates.

## Explicit non-guarantees

- no real expert-labelled known-answer suite;
- no externally operated private temporal-holdout custody;
- no production literature indexes, providers, extractors, or relation model;
- no measured real-corpus recall, relation accuracy, false novelty, or reviewer agreement;
- no domain power analysis or evidence that the initial thresholds generalize;
- no default scheduler path that automatically creates every required F8 artifact;
- no scorecard/write-up/API/UI migration to the new claim ceiling;
- no SOTA comparator existed at the F8-S5 acceptance point (subsequently added by F8-S6);
- no evidence that a current Aletheia candidate is scientifically novel;
- no completion of the F8 scientific exit or F7 real Frontier Gate.

## Next slice

Proceed to F8-S6 protocol-safe SOTA integration while keeping two release tracks explicit:

1. engineering: canonical dataset/metric/protocol signatures, comparability matrices, headline
   suppression for non-comparable results, and direction/write-up consumption;
2. scientific: commission a real expert known-answer and prospective temporal suite, freeze private
   custody, run production adapters/matcher, measure the preregistered errors, and feed the result to
   F7 L1.

## Subsequent status

F8-S6 engineering was subsequently completed on 2026-08-15. It adds pre-sealed references, signed
paired results, exact protocol matrices, Holm/practical superiority, immutable campaign decisions,
and an explicit no-fallback write-up consumer. This does not change F8-S5's scientific-exit status;
see `F8_S6_PROTOCOL_SAFE_SOTA_IMPLEMENTATION_REPORT_2026_08_15.md`.
