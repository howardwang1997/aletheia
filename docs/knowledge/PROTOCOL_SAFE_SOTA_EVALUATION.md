# F8-S6 protocol-safe SOTA evaluation guide

## Current boundary

F8-S6 provides an engineering-complete path from an authorized F8-S5 direction to a pre-sealed
reference registry, exact paired benchmark receipts, a statistically and practically controlled
comparison matrix, and a fail-closed manuscript claim.

The included references, scores, papers, reviewers, and executions are synthetic fixtures. They
prove that the software rejects important forms of protocol and headline manipulation. They do not
show that the fixture references are real SOTA, that any published method was reproduced, or that
an Aletheia candidate beats the frontier.

## Evidence flow

```text
authorized F8-S5 ResearchDirectionGate
                 |
 independent reference search + inclusion review
                 |
 pre-sealed SOTAReferenceRegistry (>=3 required references)
                 |
 frozen evaluator ------------ frozen comparison policy
                 |                         |
      reference protocols       candidate protocol frozen later
                 \                        /
          same paired evaluation partitions
                 |
 signed candidate + every-reference result receipts
                 |
 exact protocol comparability matrix
                 |
 paired one-sided sign tests + Holm + practical margin
                 |
 SOTAEvaluationCampaign
   | confirmed       | not demonstrated      | evidence blocked
   v                 v                       v
 moderate/supported  weak/refuted             weak/unverified
 manuscript headline exact-bound to protocol + metric + score
```

## 1. Freeze exact benchmark identities

Construct one `DatasetVersion`, `MetricDefinition`, and `ResourceBudgetSignature`. Use
`SOTA_REPLICATE_AGGREGATION_POLICY_SHA256` as the metric aggregation identity. Then build a
`ProtocolSignature` for every method.

Protocols must freeze no later than evaluation and include exact identities for task, data bytes,
split bytes, grouping/leakage, preprocessing, exclusions, uncertainty/statistics, resources,
external resources, and pretraining. Method identity may differ; the comparison conditions may
not. Evaluation dates may differ and remain disclosed.

Do not translate a paper's prose into a protocol silently. Retain source-span evidence for every
reported condition, and mark an unknown dimension as unavailable rather than guessing a matching
hash.

## 2. Seal the reference registry before the candidate protocol

Each `SOTAReferenceEntry` requires:

- canonical ID and reference kind;
- exact reference protocol;
- source-paper snapshot hash;
- source-span hashes supporting the reported result;
- selection evidence;
- author-excluded inclusion reviewer and review receipt;
- selection time no earlier than the reported evaluation.

The source paper must exist in the exact F8-S1 corpus bound by the direction gate, and every result
evidence span must belong to that paper. Registry construction and campaign validation both
recheck this closure.

Build the registry only from an authorized direction:

```python
registry = build_sota_reference_registry(
    registry_id="candidate-sota-registry-v1",
    direction_gate=direction_gate,
    selection_protocol_sha256=selection_protocol_sha256,
    selector_reviewer_principal_sha256s=sorted_independent_selectors,
    references=tuple(sorted(required_references, key=lambda item: item.reference_id)),
    evidence_cutoff=evidence_cutoff,
    sealed_at=sealed_at,
)
```

The direction must precede registry sealing. Evaluator freezing must also precede sealing. Registry
sealing precedes policy freezing, which precedes candidate-protocol freezing and evaluation.

The minimum of three references is not a license to stop searching after three convenient rows.
The `selection_protocol_sha256` must commit the real domain search, inclusion/exclusion, official
leaderboard, and strong-baseline policy before results are observed.

## 3. Freeze the evaluator and comparison policy

```python
evaluator = SOTAEvaluatorManifest(
    evaluator_id="independent-sota-evaluator-v1",
    evaluator_code_sha256=evaluator_code_sha256,
    score_parser_sha256=score_parser_sha256,
    aggregation_policy_sha256=SOTA_REPLICATE_AGGREGATION_POLICY_SHA256,
    statistical_policy_sha256=SOTA_STATISTICAL_POLICY_SHA256,
    minimum_replicates=10,
    receipt_key_id="evaluator-key-v1",
    frozen_at=evaluator_frozen_at,
)

policy = SOTAComparisonPolicy(
    policy_id="candidate-sota-policy-v1",
    minimum_references=3,
    minimum_replicates=10,
    minimum_practical_improvement=domain_preregistered_margin,
    aggregation_policy_sha256=SOTA_REPLICATE_AGGREGATION_POLICY_SHA256,
    statistical_policy_sha256=SOTA_STATISTICAL_POLICY_SHA256,
    evaluator_manifest_sha256=evaluator.manifest_sha256,
    frozen_at=policy_frozen_at,
)
```

The evaluator has no tools. Keep the HMAC key outside the manifest, campaign, log, and archive.
The policy's practical margin must be set in the metric's reporting unit and justified before
candidate evaluation.

## 4. Issue one signed result per method

Every method must use the same ordered repeat IDs and evaluation-partition hashes. Execution and
prediction hashes must be distinct for every method/repeat pair.

```python
candidate_receipt = issue_benchmark_result_receipt(
    result_id="candidate-result-v1",
    protocol=candidate_protocol,
    replicates=candidate_replicates,
    evaluator_manifest=evaluator,
    receipt_key=evaluator_secret_key,
    completed_at=completed_at,
)
```

Replicate ordinals start at zero and are contiguous. Scores must be finite. The helper derives the
arithmetic mean exactly. Campaign binding also checks every score against the metric's valid range.

On any execution, parsing, scoring, or infrastructure failure, issue
`issue_failed_benchmark_result_receipt`. It stores no score and hashes error detail. Never omit the
reference, retry until favorable, or substitute a published number after a reproduction failure.

## 5. Build and interpret the complete campaign

```python
campaign = build_sota_evaluation_campaign(
    campaign_id="candidate-sota-campaign-v1",
    direction_gate=direction_gate,
    registry=registry,
    policy=policy,
    evaluator_manifest=evaluator,
    candidate_protocol=candidate_protocol,
    candidate_result=candidate_receipt,
    reference_results=reference_receipts_in_registry_order,
    receipt_key=evaluator_secret_key,
    generated_at=generated_at,
)
```

There must be exactly one reference receipt for each registry entry, in registry order. Every
successful result must have the same repeat/partition pairs. Receipt IDs, receipt hashes,
execution hashes, and prediction hashes must be unique.

For each compatible row the campaign reports wins, losses, ties, one-sided exact sign-test p-value,
Holm-adjusted p-value, direction-normalized mean delta, statistical/practical flags, and the row
conclusion. Paired deltas within `1e-12` are frozen as ties; the exact integer binomial-tail routine
remains defined through the 10,000-repeat schema limit.

| Campaign verdict | Meaning | SOTA headline |
|---|---|---|
| `sota_confirmed` | every sealed row is comparable, successful, Holm-significant, and practically superior | allowed, at most moderate |
| `sota_not_demonstrated` | evidence is complete, but at least one reference was not beaten | refuted |
| `sota_blocked_evidence` | any result failed or protocol is non-comparable | unverified |

“Not demonstrated” is not “almost confirmed.” “Blocked evidence” is not a measured loss. Preserve
the distinction in reports and dashboards.

## 6. Commit and reload

```python
committed = commit_sota_evaluation_campaign(archive=archive, campaign=campaign)
loaded = load_sota_evaluation_campaign(
    archive=archive,
    ledger=committed.ledger,
    receipt_key=evaluator_secret_key,
)
```

Loading rechecks canonical bytes, object identity, derivations, and every HMAC. A wrong key or
modified receipt fails before the campaign can be consumed.

## 7. Gate a manuscript headline

For direct use:

```python
decision = screen_auditable_sota_campaign(
    campaign=loaded,
    receipt_key=evaluator_secret_key,
    expected_candidate_protocol_sha256=current_protocol.protocol_sha256,
    headline_metric=current_metric_id,
    headline_score=current_aggregate_score,
    contribution_type="performance",
)
```

The screen revalidates the campaign and signatures. Protocol, metric, and score must match the
current result exactly. Paradigm and diagnostic contributions cannot turn SOTA delta into their
headline evidence.

To use the explicit scheduler path, configure the driver with a provider:

```python
def provide_sota_campaign(request):
    campaign = load_campaign_for_protocol(request["candidate_protocol_sha256"])
    return campaign, evaluator_verification_key

driver = ExperimentDriver(
    run_id,
    auditable_sota_campaign_fn=provide_sota_campaign,
)
```

The evaluated result passed to WRITE_UP must include
`info["candidate_protocol_sha256"]`. If it is missing, the callback errors, the key is wrong, or any
identity differs, the claim ledger records `weak/unverified` and does not call the legacy SOTA
shortcut. An authorized audited headline is `moderate/supported`; it is never `strong`.

## Failure diagnosis

| Symptom | Meaning/action |
|---|---|
| `one result for every sealed reference` | issue the missing explicit success/error receipt |
| `another protocol/evaluator` | restore registry order and exact protocol/evaluator identities |
| `same paired frozen replicates` | rerun every method on identical repeat and partition identities |
| `cannot reuse execution/prediction artifacts` | retain distinct method executions and outputs |
| `non_comparable:*:dataset,split,...` | do not report a delta; align protocols or disclose no comparison |
| `not_superior:*` | the candidate did not satisfy both statistical and practical superiority |
| `candidate_protocol_identity_mismatch` | the manuscript result is not the evaluated candidate protocol |
| `headline_score_receipt_mismatch` | use the signed aggregate; do not substitute another score |
| `auditable_sota_provider_error:*` | fix the explicit provider/key; no legacy fallback is permitted |

## Current non-capabilities

- No production reference registry is included.
- No official leaderboard or published method has been reproduced by this fixture.
- Reference-search completeness and inclusion quality have not been independently measured.
- The default scheduler does not yet materialize protocols, evaluator runs, or campaigns; it only
  has the explicit injected consumption path.
- HMAC verification still requires a shared evaluator key rather than public-key attestation.
- No real domain has preregistered/powered the repeat count or practical-improvement margin.
- No current Aletheia output is thereby shown to beat SOTA.
- F8 scientific exit still requires real expert novelty calibration plus prospective production
  search, matching, reference selection, reproduction, and independent review.
