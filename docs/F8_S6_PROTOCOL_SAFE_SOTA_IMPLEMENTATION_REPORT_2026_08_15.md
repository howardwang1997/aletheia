# F8-S6 protocol-safe SOTA implementation report

- Date: 2026-08-15
- Scope: pre-sealed reference selection, signed paired evaluation, complete protocol matrix,
  statistical/practical superiority, archive, and write-up claim gating
- Engineering status: complete
- Scientific-exit status: not complete

## Outcome

F8-S6 replaces the issue-12 two-scalar comparison as the audited acceptance path for a SOTA
headline. The new path starts only after an authorized F8-S5 research direction and closes the main
software loopholes around reference selection, protocol mismatch, result omission, multiplicity,
and manuscript rebinding:

- references are independently selected and sealed before the candidate protocol/result;
- every reference carries exact source, protocol, inclusion-review, and selection evidence;
- candidate and reference scores arrive only through signed per-repeat result receipts;
- all methods use the same paired repeat and evaluation-partition identities;
- a complete matrix compares data bytes, split, metric, statistics, resources, and other protocol
  dimensions;
- exact one-sided paired sign tests are Holm-corrected across all references;
- a preregistered practical-improvement margin is required in addition to statistical evidence;
- every required reference must be comparable, successful, and beaten;
- result errors and protocol mismatches block evidence rather than becoming losses or disappearing;
- campaign rows, verdict, headline bit, and claim ceiling are mechanically re-derived;
- archive load revalidates canonical bytes, derivations, and every signature;
- the optional scheduler path exact-binds protocol, metric, score, campaign, receipt, and row hashes
  into the claim ledger and never falls back after an audited-path error.

This is an engineering result. The three references, ten repeats, scores, source papers, reviewers,
and executions in the fixture are synthetic. No real SOTA claim follows from this report.

## Research and protocol basis

The design review used primary benchmark and statistical work:

- [Show Your Work](https://aclanthology.org/D19-1224/) — test scores without model-selection and
  resource context are insufficient for fair comparison;
- [Demšar 2006](https://www.jmlr.org/papers/volume7/demsar06a/demsar06a.pdf) — paired comparison and
  multiple-comparison control;
- [MLCommons Inference submission guide](https://docs.mlcommons.org/inference/submission/) — fixed
  rules, required scenarios, submission checking, and audit;
- [OpenML benchmarks](https://docs.openml.org/benchmark/) — curated tasks, fixed splits, and
  reproducible runs;
- [The Benchmark Lottery](https://openreview.net/pdf?id=5Str2l1vmr-) — rankings depend on benchmark
  selection;
- [Matbench](https://www.nature.com/articles/s41524-020-00406-3) — fixed scientific benchmark splits
  and reference-algorithm comparison.

ADR 0015 records how these considerations map to Aletheia's threat model and why the current exact
sign/Holm/practical-margin policy was selected.

## 1. Pre-sealed reference registry

`aletheia/knowledge/sota_evaluation.py` adds `SOTAReferenceEntry` and
`SOTAReferenceRegistry`. A registry is bound to the exact F8-S5 direction gate, calibrated coverage,
reference-search session, and corpus. It requires:

- at least three required canonical references;
- a frozen selection-protocol identity;
- evidence cutoff and seal time;
- at least two author-excluded selectors;
- unique protocol, paper, entry, and review identities;
- exact result evidence spans and independent inclusion reviews;
- source-paper and result-span closure into the exact bound F8-S1 corpus;
- no future or post-sealing evidence.

The freeze order is direction decision, evaluator freeze, registry seal, policy freeze, candidate
protocol freeze, candidate evaluation, signed results, then campaign generation.

## 2. Evaluator-owned result receipts

`SOTAEvaluatorManifest` freezes code/parser/policies/repeat floor/key ID and has no tools. The
successful receipt path validates the exact protocol aggregation, minimum repeats, time order,
canonical repeat ordering, finite scores, and exact arithmetic mean. Each repeat retains partition,
execution, and prediction identities.

HMAC-SHA-256 uses a key of at least 32 bytes. The error path stores an exception class and message
hash with no raw text and no score. Campaign construction verifies every signature and rejects
missing, reordered, duplicated, unpaired, out-of-range, or artifact-reused results.

## 3. Complete comparison and statistical policy

The campaign reuses the issue-12 protocol comparator, which treats every mismatch except evaluation
date as blocking. For compatible rows it derives direction-normalized paired differences, wins,
losses, ties, an exact one-sided sign-test p-value, Holm-adjusted p-value, and mean favorable delta.
The frozen tie tolerance is `1e-12`; binomial tails use integer recurrence before final
normalization, avoiding the large-combination float overflow at high repeat counts.

A row beats its reference only when both conditions hold:

1. adjusted p-value is at most 0.05;
2. mean favorable delta is at least the frozen positive practical margin.

The global campaign confirms SOTA only if every reference row passes. A complete but unbeaten row
produces `sota_not_demonstrated/comparative_only`; failed or non-comparable evidence produces
`sota_blocked_evidence/none`; full success produces `sota_confirmed/moderate`.

The 12-repeat adversarial fixture has ten wins and two losses per row. Each nominal p-value is
`79/4096 ≈ 0.0193`, but three-row Holm adjustment yields `237/4096 ≈ 0.0579`, correctly suppressing
the headline. A separate ten-of-ten fixture with only `0.01` mean gain passes statistics but fails
the `0.05` practical margin.

## 4. Immutable campaign and write-up consumption

`SOTAEvaluationCampaign` embeds and re-derives the entire direction/registry/policy/evaluator/
protocol/result/matrix/decision closure. `commit_sota_evaluation_campaign` and
`load_sota_evaluation_campaign` use the content-addressed knowledge archive and reverify signatures.

`aletheia/research/sota_claims.py` adds a typed manuscript decision. It revalidates campaign shape
and signatures and exact-binds the current protocol hash, metric identity, and aggregate score.
Only a performance contribution can use the result as a SOTA headline. Outcomes are:

| Evidence | Claim ledger |
|---|---|
| exact-bound `sota_confirmed` | `moderate/supported` |
| exact-bound `sota_not_demonstrated` | `weak/refuted` |
| mismatch/error/non-comparable | `weak/unverified` |

`ExperimentDriver` now accepts an optional `auditable_sota_campaign_fn`. When present, WRITE_UP
uses this typed decision and campaign/receipt/row evidence hashes. Wrong provider types, provider
exceptions, missing current protocol identity, wrong key, and all binding failures fail closed with
no legacy fallback. Existing deployments without the callback retain the historical comparison
path for compatibility and cannot describe it as F8-S6 audited acceptance.

## Test evidence

Focused F8-S6 acceptance currently includes 36 tests covering:

- a real synthetic F8-S5 strong-direction-to-F8-S6 closure;
- three-reference registry floor, canonical ordering, cutoff, and author exclusion;
- rejection of source papers or result spans outside the bound corpus;
- exact evaluator policies, no tools, key length, repeat floor, and error redaction;
- evaluation-date disclosure without blocking;
- split, preprocessing, dataset-byte, metric-formula, and budget mismatches;
- lower-is-better and higher-is-better normalization;
- one unbeaten reference, Holm correction, and practical-margin failure;
- numerically stable exact sign-test tails at the 10,000-pair schema limit;
- missing/reordered receipts, invalid HMAC, explicit evaluator error, and metric-range violation;
- mismatched partitions and reused execution/prediction artifacts;
- forged matrix order and headline bit;
- archive round trip and wrong-key rejection;
- unauthorized F8-S5 directions;
- write-up protocol/metric/score/contribution rebinding;
- write-up signature revalidation, decision-bit forgery, refuted result semantics, driver
  consumption, and provider-error no-fallback behavior.

Focused and integration checks at implementation time:

```text
F8-S6 focused: 36 passed
write-up/evidence/F8 knowledge integration: 262 passed
changed Python Ruff + compilation: passed
```

Final repository acceptance on 2026-08-15:

```text
non-Docker: 917 passed, 1 skipped, 29 deselected in 532.77 s
Docker:      29 passed, 918 deselected in 45.30 s
```

These results are engineering test evidence, not scientific validation.

## Files added or materially changed

- `aletheia/knowledge/sota_evaluation.py`;
- `aletheia/knowledge/__init__.py`;
- `aletheia/research/sota_claims.py`;
- `aletheia/scheduler/driver.py`;
- `tests/knowledge/f8s6_fixtures.py`;
- `tests/knowledge/test_sota_evaluation.py`;
- `docs/adr/0015-f8-presealed-protocol-safe-sota-evaluation.md`;
- `docs/knowledge/PROTOCOL_SAFE_SOTA_EVALUATION.md`;
- this report and README/master-plan/docs-index status updates.

## Explicit non-guarantees

- no production domain reference registry or measured registry completeness;
- no official leaderboard integration or independently reproduced published method;
- no live evaluator service, KMS, or public-key result attestation;
- no real-domain power analysis or validated practical-improvement margin;
- no automatic default-scheduler materialization of protocols, repetitions, reference runs, and
  campaign artifacts;
- no migration of the API/dashboard to expose the audited matrix;
- no real expert-labelled novelty suite, private temporal custody, or production F8-S5 pass;
- no evidence that a current Aletheia candidate is novel or beats SOTA;
- no completion of F8 scientific exit or the real F7 Frontier Gate.

## Next slice

F8-S1 through F8-S6 engineering contracts are now complete. Continue on two tracks:

1. scientific F8 exit: commission the real expert known-answer/temporal suite, operate production
   retrieval/matching/reference selection, preregister real margins, reproduce the complete sealed
   matrix, and feed prospective outcomes to F7;
2. repository capability: begin F9-S1 competing causal-hypothesis graph contracts while retaining
   F7/F8 as non-negotiable release gates.
