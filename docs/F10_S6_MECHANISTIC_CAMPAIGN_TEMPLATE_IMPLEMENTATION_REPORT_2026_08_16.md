# F10-S6 mechanistic campaign template implementation report

Date: 2026-08-16
Status: Engineering template complete; real execution and scientific release blocked

## Outcome

Aletheia can now freeze and verify a complete mechanistic-campaign contract that joins an exact F8
research direction, F9 competing hypotheses/causal audit/probabilistic predictions, and at least two
distinct F10 experiment families. It can ingest each result only through the typed raw → parse →
validate → commit boundary, independently map the observation to a preregistered outcome, score
nominal and sensitivity likelihoods without a joint pseudoreplicated posterior, and emit a fully
rederived evidence bundle with an explicit claim ceiling.

This completes the F10-S6 engineering template. It does not complete a real campaign. The current
repository has no production F8 direction, ready production F9 lineage, registered confirmatory
mechanism capabilities, fresh reservation, or independent confirmation. The implementation records
that state as blocked instead of substituting synthetic fixtures or relabelling the existing
provisional computational/simulation evidence.

## Implemented contracts

### Closed F8 → F9 → F10 lineage

`MechanisticCampaignProtocol` embeds:

- the exact F9 causal campaign and, transitively, its F8 direction gate and F9 competing-hypothesis
  snapshot;
- at least two unique ready probabilistic prediction campaigns derived from that causal campaign;
- exactly one sorted experiment slot for each unique prediction campaign and experiment namespace;
- a frozen capability-registry snapshot containing every exact slot manifest;
- a frozen sensitivity/margin/aggregation policy and independent campaign evaluator; and
- mechanically derived execution and mechanism-release blockers.

The protocol rejects a different source causal campaign, reused prediction, reused namespace,
postdated source, out-of-registry manifest, non-materials capability, or evaluator principal reused
from any F9/capability/custody role.

### Capability and experiment-family boundary

The four supported families have compatible capability actions: C1 data audit, C2 computational
structure discrimination, C3 structural intervention, and C4 simulation. Action compatibility is
necessary but is deliberately not sufficient. Every slot embeds a
`MechanisticCapabilityQualification` that exact-binds the manifest, intended family, compatible
action, qualification evidence hash, independent domain reviewer, and freeze time. A generic
computational experiment therefore cannot silently become C2, and an arbitrary/reference
simulation cannot silently become campaign-qualified C4. A valid campaign requires at least two
distinct qualified families and at least one qualified C3 or C4 family.

Each slot binds the manifest, prediction campaign, input identity, data identity, executor
implementation hash, cost limit, wall-time limit, and plan time. The implementation identity must
equal the selected capability's frozen executor implementation. Registered status, confirmatory
evidence level, causal claim authority, and mechanism-capable manifest claim type are release gates,
not prerequisites for exploratory execution.

### Fresh and independent confirmation

`FreshConfirmationReservation` freezes a source identity, executor implementation, custody
principal, independence kind, and reservation time before observation access. A fresh-confirmation
slot must bind exactly that reservation. Fresh-dataset confirmation cannot reuse the internal data
identity; independent implementation cannot reuse the internal executor identity; external-site
custody cannot reuse internal capability/custody principals.

The evidence later proves only that a declared attestation hash exists and that source opening was
after both protocol freeze and reservation. Authenticated external signatures and a registry trust
root remain future work; hashes alone are not described as independent scientific custody.

### Typed evidence and mapping boundary

`MechanisticSlotEvidence` embeds a complete `CommittedCapabilityObservationPipeline`, not a scalar
metric. The evaluator checks exact capability manifest, campaign protocol hash, slot input hash,
measurement run purpose, post-freeze execution/source chronology, validated disposition, and F9
admissibility.

An `OutcomeMappingManifest` is frozen before source opening. Its implementation and principal are
explicit; the principal cannot be the campaign evaluator or reuse F9/capability scientific roles.
The observed class must already exist in the corresponding F9 outcome schema. The mapping artifact
and fresh-independence attestation remain content-addressed by hashes.

### Robust discrimination without pseudoreplication

For each slot, the evaluator looks up the likelihood of the observed bin for every competing
hypothesis under nominal predictions and every frozen sensitivity scenario. A slot passes only if:

1. every scenario is complete;
2. every scenario has a unique winner;
3. the winner is identical across scenarios; and
4. the minimum winner-versus-runner-up probability margin meets policy.

Evidence validity and scientific discrimination are separate. Broken preregistration, identity,
chronology, validation, or mapping produces `invalid_evidence`. A tie, scenario winner change, or
small probability margin leaves evidence valid but produces `inconclusive`.

Slots are never multiplied into a joint posterior. The decision uses only concordance of robust
per-slot winners and records `joint_posterior_computed=false`. Concordant null, conflicting winners,
and inconclusive results remain explicit terminal dispositions.

### Claim ceiling and tamper resistance

Concordance is not sufficient for a mechanism claim. The final ceiling intersects:

- protocol release blockers;
- confirmatory admission of every observation;
- F9 causal claim ceiling;
- registered capability claim types; and
- fresh/independent confirmation evidence.

Provisional or non-confirmatory results can reach only `bounded_pattern_supported`. A within-model
capability cannot inherit an experimental mechanism ceiling from upstream. The evidence bundle
recomputes every assessment and decision during validation; forged blocker, winner, disposition, or
claim-ceiling fields are rejected.

## Synthetic acceptance chain

The focused test builds a complete synthetic chain:

```text
synthetic strong F8 direction
  -> H0 + primary + alternative F9 campaign
  -> controlled-intervention causal audit
  -> distinct structure and simulation predictions
  -> synthetic registered + independently family-qualified C2 and C4 registry
  -> frozen internal + fresh independent-implementation slots
  -> two post-freeze content-addressed observation pipelines
  -> independent outcome mappings
  -> concordant, conflicting, invalid, provisional, and low-margin decisions
```

The positive fixture reaches `mechanism_candidate_supported` only to prove the software contract.
Every input—including the direction gate, registration receipts, measurements, validation reports,
and independence attestation—is synthetic. It is not a materials finding or evidence that a real
campaign is ready.

Adversarial coverage includes an out-of-registry manifest, insufficient experiment-family
diversity, provisional self-promotion, low-margin evidence misclassified as invalid, conflicting
robust winners, preregistration or outcome-schema rebinding, and derived decision tampering.

## Current frozen readiness audit

The CLI `scripts/mechanistic_campaign_readiness.py` rebuilds a fail-closed audit from a registry and
can require either execution or scientific-release readiness with nonzero exit status. The frozen
current audit is
`configs/materials/f10_mechanistic_campaign_readiness_audit_v1.json`.

| Object | Identity |
|---|---|
| registry snapshot | `80ea6dfa5c250dbdb76a4b3b38ceb7460580d17d7cdb47695da93ff38930ad77` |
| readiness audit object | `d7fe32533ad2ea9853c35a56555d816f27b489e532a47cf6a29a10c7a89d003b` |
| readiness JSON bytes | `00ab47039424c722c3b7eaabb54f8f44985bcfe157593eddda19ff26f4f0a72c` |
| execution ready | `false` |
| scientific release ready | `false` |

The audit resolves the latest registry entries
`materials.band_gap.range_compression@2.1.0` and
`materials.simulation.ase_emt_eos_reference@1.0.0`, but correctly assigns `family=null` to both:
neither has an independently frozen F10-S6 family qualification. Both are also provisional. The
audit records eight blockers:

1. production F8 direction missing;
2. ready F9 hypothesis campaign missing;
3. ready F9 causal campaign missing;
4. fewer than two registered confirmatory families;
5. registered intervention or simulation missing;
6. registered mechanism-capable capability missing;
7. fresh-confirmation reservation missing; and
8. independent confirmation missing.

## Frozen implementation identities

| Artifact | SHA-256 |
|---|---|
| `aletheia/domains/materials/mechanistic_campaign.py` | `cbaf1b6631a3b29c3eab823031fd9300c33f898f1d5e032e7a0a7ced032c0d06` |
| `scripts/mechanistic_campaign_readiness.py` | `0c003c62052c27d051b74663fb7dd1353ce70ebf0da4ab7d3ff03dafbd14f45a` |
| focused test module | `e8a759d2b4547b9f5b97142c31aeb3c69c55ad2abc5812d1ed9b8a1e5de646e4` |

These hashes describe this engineering freeze. They are not signed supply-chain attestations.

## Verification

At the initial F10-S6 implementation freeze:

- Ruff passes for the campaign module, readiness CLI, materials public exports, and focused tests;
- `tests/domains/materials/test_mechanistic_campaign.py`: 13 passed in 7.52 seconds; and
- combined `tests/domains/materials tests/capabilities`: 77 passed with 2,611 upstream spglib
  deprecation warnings in 14.36 seconds;
- authoritative full non-Docker regression: 1,214 passed, 1 skipped, 29 deselected, and 2,611
  upstream spglib warnings in 814.82 seconds; and
- the frozen current readiness JSON exactly revalidates and has the expected object/byte hashes.

Docker tests were deliberately deselected because F10-S6 adds a composition/evaluation template,
not a new container executor. F10-S5's two retained digest-pinned container runs remain the current
simulation runtime evidence and are not counted as fresh or independent S6 confirmation.

## Scientific interpretation and limits

The correct current statement is: Aletheia has an executable and adversarially tested schema for a
two-family, precommitted mechanistic campaign, plus a machine-readable proof that the present
materials repository is not ready to run or release such a campaign.

It has not selected a production materials question, run a fresh mechanism-discriminating
intervention, obtained independent implementation/dataset/site evidence, changed a real hypothesis
set, or passed domain-expert review. The existing Matbench structure result and ASE/EMT reference
remain separate public/provisional exploratory artifacts, lack F10-S6 family qualifications, and
cannot be combined retrospectively into an F10-S6 scientific exit.

## Remaining work

- supply a production F8 direction whose eventual observations were unavailable at freeze time;
- execute F9 competing-hypothesis, causal, and distinct prediction campaigns for that exact question;
- independently qualify, review, and promote at least two relevant capabilities, including a C3/C4
  mechanism action, through the future signed F10-S7 authorization boundary;
- implement authenticated reservation, source custody, and domain-specific dataset/sample-to-input
  binding;
- execute fresh confirmation followed by an independent dataset, implementation, or external site;
- preserve a real negative/conflicting result and make it change or constrain the hypothesis set;
- obtain domain-expert review of physical feasibility, convergence, measurement, mapping, and claim
  scope; and
- evaluate the resulting private materials quest against the composition-only and K2 baselines
  required for F10 scientific exit.
