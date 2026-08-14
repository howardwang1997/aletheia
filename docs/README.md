# Aletheia Docs

This directory contains the architecture, project review, roadmap, and implementation RFCs for
Aletheia.

Recommended reading order:

1. `ARCHITECTURE.md` — current system architecture and invariants.
2. `PROJECT_REVIEW.md` — neutral assessment of strengths, risks, and gaps.
3. `FRONTIER_SCIENTIST_F7_F12_DETAILED_PLAN_2026_08_13.md` — current executable master plan from
   the completed Real Campaign Gate baseline to a domain-level autonomous frontier scientist.
4. `PF1_PF2_F7S1_IMPLEMENTATION_REPORT_2026_08_13.md` — implementation and migration/restore
   evidence for the first Frontier Scientist foundation slice.
5. `F7S2_INDEPENDENT_RUNNER_IMPLEMENTATION_REPORT_2026_08_13.md` — independent evaluator runner,
   append-only attempt ledger, resource enforcement, submission validation, and signed receipts.
6. `F7_ISSUE_6_SCIENCEAGENTBENCH_IMPLEMENTATION_REPORT_2026_08_14.md` — frozen licensed mini-suite,
   two-plane official objective scorer, reproducibility, and adversarial acceptance evidence.
7. `benchmarks/SCIENCEAGENTBENCH_ADAPTER.md` — operator runbook for acquiring assets, building the
   reviewed runtime, preparing a suite, and interpreting verdicts.
8. `F7_ISSUE_7_COREBENCH_IMPLEMENTATION_REPORT_2026_08_14.md` — sanitized public-repository
   boundary, Asta CORE-Bench-Hard objective/artifact scorer, and acceptance evidence.
9. `benchmarks/COREBENCH_ADAPTER.md` — operator runbook for the frozen public validation subset,
   licensed capsules, offline image, suite preparation, and verdict semantics.
10. `F7_ISSUE_8_DISCOVERYWORLD_IMPLEMENTATION_REPORT_2026_08_14.md` — two-container hidden world,
    objective action trace, information gain, explicit rule discovery, and real Docker evidence.
11. `benchmarks/DISCOVERYWORLD_ADAPTER.md` — operator runbook for building both images, freezing the
    public suite, authoring a policy, and interpreting trajectory/verdict evidence.
12. `F7_ISSUE_9_BASELINE_MATRIX_IMPLEMENTATION_REPORT_2026_08_14.md` — same-model four-arm
    preregistration, paired execution/statistics, no-best-of-N ledger reconciliation, and evidence.
13. `benchmarks/BASELINE_MATRIX.md` — evaluator operator contract for materializing, running, and
    aggregating direct/generic/no-K2/full-K2 comparisons.
14. `F7_ISSUE_10_PRIVATE_SUITE_IMPLEMENTATION_REPORT_2026_08_14.md` — prospective custody,
    role-separated encryption, one-time unlock, contamination, verified cleanup, and retirement.
15. `benchmarks/PRIVATE_SUITE_CUSTODY.md` — evaluator-owner runbook for commissioning, sealing,
    authorizing, materializing, running, closing, contaminating, retiring, and auditing private
    suites.
16. `adr/0007-private-suite-custody-and-retirement.md` — threat model, custody decision, guarantees,
    and explicit non-guarantees.
17. `F7_ISSUE_11_FRONTIER_GATE_REPORT_IMPLEMENTATION_REPORT_2026_08_14.md` — validation-calibrated
    thresholds, frozen claims, raw receipt reaggregation, custody-linked verdicts, and evidence.
18. `benchmarks/FRONTIER_GATE_REPORT.md` — evaluator runbook for calibrating, freezing, indexing
    evidence, and issuing immutable JSON/Markdown/SVG PASS/FAIL/BLOCKED artifacts.
19. `adr/0008-validation-calibrated-frontier-gate-report.md` — two-stage freeze, decision semantics,
    evidence boundary, and explicit scientific non-claims.
20. `F8_ISSUE_12_KNOWLEDGE_SCHEMA_SPIKE_REPORT_2026_08_14.md` — immutable temporal corpus,
    source-span, search/coverage, claim/prior-art, novelty, and protocol-safe SOTA contracts.
21. `benchmarks/KNOWLEDGE_BOUNDARY_SCHEMA.md` — developer guide to the issue-12 object boundary,
    synthetic temporal fixture, guarantees, tests, and explicit non-capabilities.
22. `adr/0009-f8-knowledge-boundary-schema-spike.md` — temporal false-novelty threat model,
    literature-as-untrusted-data decision, schema boundary, and rejected alternatives.
23. `F8_S1_CORPUS_PERSISTENCE_IMPLEMENTATION_REPORT_2026_08_14.md` — license-explicit provider
    receipts, immutable ordered PostgreSQL persistence, conflict/rollback, triggers, and CLI.
24. `knowledge/CORPUS_PERSISTENCE.md` — operator/developer guide for migration, bundle schema,
    access-right rules, validation, persistence, inspection, recovery, and current limits.
25. `adr/0010-f8-immutable-corpus-persistence-and-access-rights.md` — official rights/selector
    evidence, hash-only storage decision, relational layout, trust boundary, and alternatives.
26. `F8_S2_SEARCH_REPLAY_CITATION_IMPLEMENTATION_REPORT_2026_08_14.md` — deterministic query axes,
    metadata-only content-addressed responses, complete failure ledgers, replay, citation rounds,
    and fail-closed coverage derivation.
27. `knowledge/SEARCH_REPLAY_AND_CITATION.md` — developer guide for freezing plans/adapters,
    executing and committing, replaying, traversing citations, and building coverage evidence.
28. `adr/0011-f8-deterministic-search-replay-and-citation-traversal.md` — provider/API constraints,
    metadata retention boundary, mechanical frontier decision, and explicit non-capabilities.
29. `F8_S3_CLAIM_EXTRACTION_IMPLEMENTATION_REPORT_2026_08_15.md` — licensed ephemeral span
    access, strict structured claims, confidence review, immutable graph closure, and replay.
30. `knowledge/CLAIM_EXTRACTION_AND_REVIEW.md` — developer guide for manifests, content resolvers,
    extraction, review resolution, graph construction, commitment, and replay.
31. `adr/0012-f8-licensed-atomic-claim-extraction-and-independent-review.md` — related scientific
    NLP evidence, authorization/trust decision, review semantics, and explicit non-capabilities.
32. `F8_S4_PRIOR_ART_MATCHING_IMPLEMENTATION_REPORT_2026_08_15.md` — four-channel complete-union
    recall, no-delete reranking, strict relation/component differences, review, and ledgers.
33. `knowledge/PRIOR_ART_MATCHING_AND_REVIEW.md` — developer guide for recall/matcher manifests,
    union semantics, execution, stage failures, relation review, commitment, and current limits.
34. `adr/0013-f8-auditable-multichannel-prior-art-matching.md` — related retrieval evidence,
    no-delete decision, relation/review semantics, consequences, and rejected alternatives.
35. `F8_S5_CALIBRATED_NOVELTY_IMPLEMENTATION_REPORT_2026_08_15.md` — evaluator-owned temporal
    calibration, confidence-bound acceptance, artifact-derived coverage, review, and direction gate.
36. `knowledge/CALIBRATED_NOVELTY_ACCEPTANCE.md` — developer/operator guide for suite sealing,
    receipts, metrics, live evidence derivation, authorship, independent review, and gate use.
37. `adr/0014-f8-calibrated-novelty-acceptance-and-direction-gate.md` — related benchmark evidence,
    statistical/coverage/review decisions, limitations, consequences, and rejected alternatives.
38. `F8_S6_PROTOCOL_SAFE_SOTA_IMPLEMENTATION_REPORT_2026_08_15.md` — pre-sealed references,
    signed paired results, complete comparison matrix, Holm/practical superiority, and write-up gate.
39. `knowledge/PROTOCOL_SAFE_SOTA_EVALUATION.md` — developer/evaluator guide for registry sealing,
    exact protocols, receipt issuance, campaign interpretation, archive, and claim consumption.
40. `adr/0015-f8-presealed-protocol-safe-sota-evaluation.md` — benchmark evidence, threat model,
    selection/evaluation/statistical decisions, consequences, and rejected alternatives.
41. `F9_S1_WORLD_MODEL_VERSIONING_IMPLEMENTATION_REPORT_2026_08_15.md` — stable lineages,
    immutable versions, closed competing beliefs, PostgreSQL persistence, and K2 compatibility.
42. `epistemics/WORLD_MODEL_VERSIONING.md` — developer/operator guide for constructing, revising,
    storing, recovering, and reading F9 snapshots and legacy K2 projections.
43. `adr/0016-f9-immutable-competing-world-model-versioning.md` — provenance/model-workflow basis,
    version decisions, K2 non-migration boundary, consequences, and rejected alternatives.
44. `F9_S2_COMPETING_HYPOTHESIS_GENERATION_IMPLEMENTATION_REPORT_2026_08_15.md` — exact F8-bound
    request, independent complete semantic pair ledger, explicit duplicate provenance, pairwise
    prediction-discrimination proof, sanitized failures, and F9-S1 admission evidence.
45. `epistemics/F8_GROUNDED_HYPOTHESIS_GENERATION.md` — developer/operator guide for manifests,
    adapters, grounding and duplicate gates, campaign execution, archive, persistence, and limits.
46. `adr/0017-f9-f8-grounded-competing-hypothesis-admission.md` — related-work basis, proposer versus
    admission boundary, complete-pair and discrimination decisions, consequences, and alternatives.
47. `F9_S3_CAUSAL_CONTRACT_IDENTIFICATION_IMPLEMENTATION_REPORT_2026_08_15.md` — typed graphs,
    exact F8/F9 binding, structural and back-door witnesses, independent assumption review, bounded
    claim ceilings, failure/archive semantics, and acceptance evidence.
48. `epistemics/CAUSAL_CONTRACT_AND_IDENTIFICATION_AUDIT.md` — developer/operator guide for causal
    objects, graph rules, back-door audit, assumption review, dispositions, archive, and limits.
49. `adr/0018-f9-explicit-causal-contract-and-bounded-identification.md` — causal-inference basis,
    exact implemented criterion, proposer/reviewer/harness split, consequences, and alternatives.
50. `F9_S4_PREOBSERVATION_PREDICTION_COMMITMENT_IMPLEMENTATION_REPORT_2026_08_15.md` — frozen
    experiment/outcome protocols, calibrated likelihood family, admission probes, immutable receipt,
    observation seal, mutation violation, and acceptance evidence.
51. `epistemics/PREOBSERVATION_PREDICTION_COMMITMENT.md` — developer/operator guide for outcome
    schemas, calibration, probability/ordinal semantics, execution, archive, and observation staging.
52. `adr/0019-f9-pre-observation-prediction-commitment-and-observation-seal.md` — preregistration and
    forecast-verification basis, exact trust boundary, consequences, and rejected alternatives.
53. `AUTONOMOUS_RESEARCH_ROADMAP.md` — historical 3-month, 6-month, and long-term improvement plan.
54. `RFC_GUARDRAILS_AND_EVIDENCE.md` — historical engineering RFC for guardrails and evidence.
