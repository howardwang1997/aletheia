# F8-S5 calibrated novelty acceptance guide

## Current boundary

F8-S5 now provides an engineering-complete, content-addressed path from evaluator-owned calibration
through artifact-derived live coverage to independent novelty review and a research-direction gate.
The path is optionally consumable by `aletheia.research.discovery.discover` when the caller supplies
an exact gate callback.

The repository does **not** contain a real expert-labelled known-answer suite, a privately operated
temporal holdout, production literature adapters, or a measured domain false-novelty result. The 80
cases and 240 trial receipts in tests are deterministic synthetic fixtures. They validate protocol
invariants; they do not show that Aletheia can determine novelty accurately in real science.

## Evidence flow

```text
evaluator-owned validation labels + later temporal-holdout labels
                            |
                   sealed label commitment
                            |
             base + >=2 semantics-preserving variants
                            |
          exact F8-S4 resolution + F8-S2 search per trial
                            |
                  signed trial receipt for every variant
                            |
      one-sided confidence bounds on recall/errors/stability
                            |
                NoveltyCalibrationReport PASS/FAIL
                            |
                            v
 F8-S1 ingestion + F8-S2 campaign + F8-S3 graph + F8-S4 resolution
                    + correction/retraction report
                            |
          six external coverage signals derived, never supplied
                            |
            CalibratedNoveltyCoverageAssessment
                            |
       frozen authorship -> exact novelty evidence package
                            |
     domain-expert + research-librarian author-excluded review
                            |
                 ReviewedNoveltyDecision
                            |
            mechanical ResearchDirectionGate
                            |
  advance strong | advance bounded | reject known | research more | block
```

## 1. Build the evaluator-owned calibration suite

Freeze `NoveltyCalibrationPolicy` before constructing cases. The default policy requires:

- 40 validation and 40 later temporal-holdout cases;
- at least 30 known non-strong and 10 strong-novel labels per split;
- a base variant and at least two semantics-preserving perturbations per case;
- one-sided 95% Wilson confidence bounds;
- exact validation-and-holdout threshold application;
- evaluator-only holdout labels and candidate-author exclusion.

Each `NoveltyCalibrationCase` freezes the temporal cutoff, corpus snapshot, candidate authors,
variant claim/graph/search identities, and input evidence. Validation cutoffs must all precede the
first temporal-holdout cutoff.

```python
suite = build_novelty_calibration_suite(
    suite_id="domain-novelty-v1",
    policy=policy,
    system_manifest_sha256=matcher_manifest_sha256,
    cases=cases,
    labels=evaluator_only_labels,
    holdout_custody_manifest_sha256=custody_manifest_sha256,
    sealed_at=sealed_at,
)
```

The returned suite contains only `labels_commitment_sha256`; the report later receives the exact
labels and rechecks that commitment. This is not a substitute for production secret custody.

## 2. Issue exactly one signed receipt per variant

The evaluator manifest freezes evaluator code, relation parser, the exact classification policy,
and the receipt key identifier. It has no tools.

```python
receipt = issue_calibration_trial_receipt(
    suite=suite,
    case=case,
    variant=variant,
    resolution=prior_art_resolution,
    search_session=search_session,
    evaluator_manifest=evaluator_manifest,
    receipt_key=secret_key_bytes,
    completed_at=completed_at,
)
```

The successful helper verifies the exact matcher manifest, graph bundle, corpus, candidate claim,
search protocol, and evidence times. It projects reviewed relations into a minimal evaluator view
and applies `NOVELTY_CLASSIFICATION_POLICY_SHA256` mechanically.

Use `issue_failed_calibration_trial_receipt` after any exception. It stores the exception class and
a message hash. Never drop a failed trial. Keys shorter than 32 bytes are rejected and keys never
enter the report or archive.

## 3. Build and interpret the calibration report

```python
report = build_novelty_calibration_report(
    report_id="domain-calibration-v1",
    suite=suite,
    evaluator_manifest=evaluator_manifest,
    labels=evaluator_only_labels,
    trial_receipts=receipts_in_exact_case_variant_order,
    receipt_key=secret_key_bytes,
    generated_at=generated_at,
)
```

The builder verifies every HMAC before deriving metrics. Trial IDs, receipts, resolutions, and
search sessions must be unique. Missing, reordered, duplicated, reused, or pre-sealing trials fail.

For minimum-quality metrics, compare the one-sided lower bound; for false/missed strong novelty,
compare the one-sided upper bound. A `PASS` requires every threshold on both splits and zero trial
errors. Point estimates are retained for diagnosis but do not control acceptance.

Commit and reload with:

```python
committed = commit_novelty_calibration_report(archive=archive, report=report)
loaded = load_novelty_calibration_report(
    archive=archive,
    ledger=committed.ledger,
    receipt_key=secret_key_bytes,
)
```

Load revalidates canonical JSON, object identity, all derivations, and every signature.

## 4. Derive live coverage from exact artifacts

Do not call the older F8-S2 coverage builder with hand-authored observations for an F8-S5 novelty
decision. Use the calibrated builder:

```python
coverage = build_calibrated_novelty_coverage_assessment(
    assessment_id="candidate-coverage-v1",
    calibration_report=report,
    calibration_receipt_key=secret_key_bytes,
    ingestion_bundle=ingestion_bundle,
    claim_graph_bundle=claim_graph_bundle,
    prior_art_resolution=prior_art_resolution,
    correction_report=correction_report,
    campaign=citation_campaign,
    policy_frozen_at=policy_frozen_before_live_search,
    generated_at=generated_at,
)
```

This API has no `external_observations` parameter. It derives:

| Signal | Exact source |
|---|---|
| known-answer recall | temporal-holdout one-sided lower bound |
| seed recovery | frozen seeds intersecting replayed live search hits |
| full-text availability | exact grants for distinct resolved prior papers |
| source-span verification | resolved relation spans in the ingested corpus |
| correction/retraction check | bound complete report over every resolved prior paper |
| perturbation stability | temporal-holdout one-sided lower bound |

F8-S2 continues to derive the other four signals from its complete campaign. A failed calibration,
any hard signal failure, or fewer than three accepted prior relations for any candidate makes
`decision_verdict=coverage_insufficient`.

## 5. Freeze candidate authorship and assemble the review package

```python
authorship = build_candidate_authorship_manifest(
    manifest_id="candidate-authors-v1",
    coverage=coverage,
    candidate_claim_sha256s=(candidate_claim_sha256,),
    author_principal_sha256s=sorted_author_principals,
    authorship_evidence_sha256=authorship_receipt_sha256,
    frozen_at=frozen_before_package,
)

package = build_novelty_evidence_package(
    package_id="candidate-novelty-package-v1",
    coverage=coverage,
    authorship_manifest=authorship,
    candidate_claim_sha256=candidate_claim_sha256,
    temporal_limitations=temporal_limitations,
    model_prior_limitations=model_prior_limitations,
    contamination_disclosure=contamination_disclosure,
    assembled_at=assembled_at,
)
```

The package classification and exact differences are not inputs. They are derived from reviewed
relations by the same classifier used in calibration. Coverage insufficiency forces the
indeterminate class.

## 6. Collect role-distinct, author-excluded reviews

Construct `CalibratedNoveltyReview` records against `package.package_sha256`. Reviews include a
reviewer principal, credential hash, role, rationale hash, and attestation receipt. Canonical order
is by review ID.

For direction authorization, confirmed reviews must include both:

- `domain_expert`;
- `research_librarian`.

Two domain experts do not satisfy the retrieval-review role. Candidate authors cannot review.
`request_more_search` and `reject_classification` remain explicit blockers.

## 7. Derive assessment, claim ceiling, and direction

```python
decision = build_reviewed_novelty_decision(
    decision_id="candidate-novelty-decision-v1",
    assessment_id="candidate-novelty-assessment-v1",
    coverage=coverage,
    calibration_receipt_key=secret_key_bytes,
    authorship_manifest=authorship,
    evidence_package=package,
    independent_reviews=reviews,
    generated_at=generated_at,
)

gate = build_research_direction_gate(
    gate_id="candidate-direction-gate-v1",
    novelty_decision=decision,
    calibration_receipt_key=secret_key_bytes,
    decided_at=decided_at,
)
```

The mechanical outcomes are:

| Evidence state | Disposition | Experiment | Novelty ceiling |
|---|---|---:|---|
| insufficient coverage/calibration | blocked indeterminate | no | speculative |
| equivalent or known special case | reject known | no | none |
| unresolved search/review | research more | no | class-dependent cap |
| confirmed incremental/contradictory | advance bounded | yes | weak |
| confirmed strong class, every prerequisite | advance strong | yes | moderate |

No F8-S5 path emits a `strong` or unbounded novelty claim.

## 8. Use the auditable discovery callback

The discovery loop remains backward compatible. To use F8-S5, each candidate must carry its exact
atomic claim hash and the caller must return the corresponding gate:

```python
survivors, rows = discover(
    ...,
    auditable_novelty_gate_fn=lambda candidate: load_gate_for(
        candidate["candidate_claim_sha256"]
    ),
)
```

The gate must be a real `ResearchDirectionGate`, cover exactly that single candidate claim, have
sufficient calibrated coverage, and authorize the experiment. Wrong types, callback exceptions,
identity mismatch, known prior art, and blockers all fail closed. The old literature-count/critic
path is retained for compatibility and must not be described as F8-S5 acceptance.

## Current non-capabilities

- No production known-answer or temporal-holdout dataset is included.
- No real expert adjudication or private label custody was run.
- No production lexical/vector/citation/entity indexes or relation judge were calibrated.
- Thresholds have not been powered or validated for a scientific domain.
- The default scheduler does not yet materialize all F8 artifacts automatically for every idea;
  integration is through the explicit callback.
- Scorecard and write-up consumers have not yet been migrated to read the F8-S5 claim ceiling.
- No result in the repository is thereby shown novel.

Those are scientific and integration gates, not implications of passing the synthetic test suite.
