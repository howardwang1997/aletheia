# F9-S7 independent K3 acceptance

## What is available

F9-S7 independently reconstructs an engineering verdict from committed F9-S5/F9-S6 artifacts and a
physically archived evidence ledger. It receives no raw observation and does not ask a model or the
scheduler to grade its own work.

```text
ordered committed F9 rounds
  + selected-experiment archives
  + validation/update archives
  + versions/revisions/claims/contradictions/terminal evidence ledger
  -> deterministic no-tool independent scorer
  -> physical reload and equality check for every archive object
  -> 11 canonical lineage/science/persistence checks
  -> accepted / partial / rejected-integrity / blocked-execution
  -> canonical content-addressed acceptance campaign
```

The core implementation is `aletheia.epistemics.acceptance`. The scheduler-facing
`aletheia.scheduler.k3_acceptance.score_k3` delegates to that exact scorer. This is an isolated
artifact-chain acceptance surface, not the hidden-world or real-materials F9 scientific exit.

## Scorer boundary

`K3AcceptanceScorerManifest` freezes:

- deterministic scorer code and output-schema hashes;
- scorer principal;
- no ambient tools;
- `observation_access="committed_artifacts_only"`; and
- freeze time no later than the first F9-S5 selection commitment.

The scorer principal must be distinct from evidence persistence and terminal-decision principals,
plus every hypothesis generator/reviewer, causal author/reviewer, prediction author/calibration
evaluator, experiment assessor/selector harness, observation validator/validation harness, and
belief-update harness in every round.

The scorer API accepts only committed wrappers and content-addressed archive handles. There is no
raw-payload or observation-store parameter.

## Round model

`K3RoundEvidence` currently represents one selected experiment:

- contiguous ordinal and stable round ID;
- one committed F9-S5 campaign with `ready_selected` disposition;
- exactly one committed F9-S6 validation attempt; and
- zero or one committed F9-S6 update attempt.

The validation must bind the round's selection receipt. Any update must bind the same selection.
Across multiple rounds, the scorer requires a single question/belief lineage and the next selection's
source snapshot to equal the previous successful update's child snapshot.

The one-validation-per-round limit is deliberate in this slice. A future retry protocol needs an
explicit authoritative-attempt relation rather than allowing several successful validations of the
same raw observation to become several updates.

## Evidence ledger

`K3EvidenceLedger` is a canonical persistence bundle containing exact sets of:

- selection, validation, and update receipts;
- source and child world-model snapshots;
- source and child belief states;
- source and revised hypothesis versions;
- source and revised prediction versions;
- hypothesis/world revision directives;
- open contradictions;
- full revision materializations;
- mechanism-claim attempts; and
- one terminal decision with reasons and evidence.

`build_k3_evidence_ledger` derives the complete hash sets from round artifacts and materializations.
`commit_k3_evidence_ledger` writes canonical JSON to a content-addressed archive. The acceptance
request embeds its committed wrapper, and the scorer physically reopens it before checking
completeness.

This proves persistence in the supplied evidence archive. F9-S8 separately binds that accepted
evidence to an atomic PostgreSQL world-model transition, typed scheduler event, and next-round
consumer; see `TRANSACTIONAL_WORLD_MODEL_CONTINUATION.md`.

## Building an acceptance request

```python
from aletheia.epistemics import (
    K3RoundEvidence,
    build_k3_acceptance_request,
    build_k3_evidence_ledger,
    commit_k3_evidence_ledger,
    run_k3_acceptance,
)

round_evidence = K3RoundEvidence(
    round_id="round.001",
    ordinal=1,
    committed_selection=committed_selection,
    committed_validations=(committed_validation,),
    committed_updates=(committed_update,),
)

evidence = build_k3_evidence_ledger(
    ledger_id="k3-evidence-001",
    rounds=(round_evidence,),
    revision_materializations=revision_materializations,
    mechanism_claims=mechanism_claims,
    terminal_decision=terminal_decision,
    persistence_principal_sha256=persistence_principal_sha256,
    persisted_at=persisted_at,
)
committed_evidence = commit_k3_evidence_ledger(
    archive=evidence_archive,
    evidence=evidence,
    committed_at=evidence_committed_at,
)

request = build_k3_acceptance_request(
    acceptance_id="k3-acceptance-001",
    rounds=(round_evidence,),
    committed_evidence_ledger=committed_evidence,
    scorer_manifest=scorer_manifest,
    policy=policy,
    selection_archive_custody_sha256=selection_custody,
    validation_archive_custody_sha256=validation_custody,
    update_archive_custody_sha256=update_custody,
    evidence_archive_custody_sha256=evidence_custody,
    issued_at=issued_at,
)

campaign = run_k3_acceptance(
    campaign_id="k3-acceptance-001",
    policy=policy,
    scorer_manifest=scorer_manifest,
    request=request,
    selection_archive=selection_archive,
    validation_archive=validation_archive,
    update_archive=update_archive,
    evidence_archive=evidence_archive,
)
```

Policy/scorer freeze, role independence, question/lineage identity, evidence commitment, and request
chronology are checked before execution.

## Physical verification

For each round, the scorer reloads:

1. the F9-S5 selection campaign;
2. the F9-S6 observation-validation campaign; and
3. the F9-S6 belief-update campaign when present.

It then reloads the evidence ledger. Canonical bytes are rehashed and Pydantic validation reruns the
complete nested F8/F9 derivation. Embedded and loaded objects must be equal.

Failure kinds identify the first untrusted boundary:

- `selection_archive_invalid`;
- `validation_archive_invalid`;
- `update_archive_invalid`; or
- `evidence_ledger_archive_invalid`.

A blocked campaign retains no round verification, evidence verification, or partial checks. Error
class and detail hash are retained, not raw archive/error content.

## The eleven checks

| Check | Required invariant |
|---|---|
| competing hypotheses | active null + primary + alternative set, minimum size, no duplicate normalized statements/versions |
| pre-observation chronology | prediction/selection before observation; staging before validation; validation before update |
| valid-observation/update bijection | one update attempt for each validated confirmation, none for rejected data |
| high-belief discrimination | selected likelihood separates at least two hypotheses above the belief floor by frozen TV threshold |
| belief lineage | exact question/lineage, parent belief hash, `version + 1`, unchanged source hypothesis/assumption/prediction objects |
| mechanism claim gate | issued claims stay under causal ceiling; mechanism claims robustly dominate every alternative in every sensitivity case |
| negative-result revision | primary negative produces append-only narrow/retire materialization and new predictions |
| contradiction retention | every derived contradiction kind/identity is persisted exactly |
| persistence completeness | every attempt, version, directive, contradiction, and required materialization appears exactly once |
| terminal decision | final action/reasons follow the last F9-S6 world directive and postdate final evidence |
| positive validated update | at least one valid observation produced `updated_robust` or `updated_fragile` |

Each `K3AcceptanceCheck` stores status (`pass`, `fail`, or `not_applicable`), canonical reason codes,
evidence hashes, and optional observed/required counts or metric/threshold.

No mechanism-claim attempt and no primary-negative result are valid `not_applicable` states. They do
not create a vacuous scientific-exit pass because high-belief discrimination and a positive
validated update remain mandatory.

## Dispositions

| Disposition | Meaning |
|---|---|
| `accepted` | integrity spine is intact; selected experiment discriminates high-belief rivals; at least one validated update succeeded |
| `partial_no_scientific_exit` | spine intact, but no successful update or high-belief discrimination exit gate failed |
| `rejected_integrity` | chronology, update mapping, lineage, claim, revision, contradiction, persistence, or terminal invariant failed |
| `blocked_execution` | a physical archive could not be trusted; no scoring performed |

`accepted` means the supplied committed evidence chain satisfies F9-S7 engineering policy. It does
not establish hidden-world superiority, a real causal mechanism, calibration, replication, novelty,
or publication readiness.

## High-belief discrimination

For the selected F9-S4 likelihood and each pair of hypotheses whose current prior probability meets
`high_belief_probability_floor`, the scorer computes:

\[
TV(P_i,P_j)=\frac{1}{2}\sum_y |P(y\mid h_i)-P(y\mid h_j)|.
\]

Every round must contain at least two high-belief hypotheses and at least one pair whose TV meets
`minimum_high_belief_pairwise_total_variation`. This is distinct from F9-S5's candidate feasibility
audit: F9-S7 explicitly checks discrimination among rivals that currently matter to belief.

## Mechanism-claim gate

`MechanismClaimRecord` is either `withheld` or `issued`, and exact-binds a round, update receipt,
hypothesis, requested F9-S3 causal ceiling, evidence, decision time, and optional issued artifact.

Descriptive or association claims must stay within the F9-S3 ceiling. An issued
`within_model_causal_only` or `causal_candidate` claim additionally requires:

- a robust update (`updated_robust`);
- stable `continue_current_set` world revision;
- non-null mechanistic target retained after the result;
- target posterior above the policy floor in nominal and all likelihood-sensitivity posteriors;
- every other hypothesis below the exclusion ceiling in nominal and all sensitivity posteriors; and
- decision time after the committed source update.

An unauthorized claim can be safely recorded as withheld. Issuing it makes the chain
`rejected_integrity`.

## Negative-result materialization

For every `narrow` or `retire` directive, the evidence ledger must contain exactly one
`K3RevisionMaterialization`.

A revised hypothesis must preserve run/question/hypothesis/role identity, use the exact parent,
increment version by one, and adopt `narrowed` or `retired` lifecycle as directed. Source objects are
never overwritten.

Narrowing must also revise the exact source prediction set. Every prediction child must preserve its
stable ID, bind the revised hypothesis version, use its exact prediction parent, increment version,
and change a testable field. A statement/rationale rewrite with identical observable, outcome,
direction, discrimination targets, and measurement protocol is rejected. Retirement cannot create
new predictions.

## Terminal decision

`K3TerminalDecision` persists final round, action, reasons, evidence, principal, and time. After a
successful update it also binds the update receipt and F9-S6 world-revision directive.

| F9-S6 world action | Accepted terminal actions |
|---|---|
| `continue_current_set` | `continue_research`, `stop_and_archive` |
| `fork_hypothesis_set` | `fork_hypothesis_set`, `stop_and_archive` |
| `seek_new_measurement_or_stop` | `seek_new_measurement`, `stop_and_archive` |
| no successful update | `stop_and_archive` only |

The scorer verifies the decision; it does not execute it.

## Replay and tamper detection

`K3AcceptanceCampaign` model validation revalidates its request and recomputes all eleven checks and
the final disposition. Forged statuses, thresholds, reasons, evidence, or verdicts fail validation.

`commit_k3_acceptance_campaign` and `load_k3_acceptance_campaign` provide canonical
content-addressed storage and explicit commit time. Both acceptance and evidence-ledger archive byte
tampering are covered by tests.

## Current limitations

- All F9-S7 fixtures are synthetic; `accepted` proves the machinery, not a scientific result.
- The evidence ledger itself remains a content-addressed attestation; F9-S8 adds the separate
  physically verified PostgreSQL transition and event projection.
- Only one validation attempt is modeled per round; retries need an explicit authoritative-attempt
  contract.
- F9-S8 feeds an authorized child snapshot into a new F9-S3–S7 chain. A retirement still requires a
  future F9-S2 hypothesis-set fork/replenishment protocol.
- Mechanism-claim artifacts are represented by hashes; manuscript/claim text is not parsed here.
- High-belief and claim thresholds are frozen engineering policy, not cross-domain calibration.
- A fragile update can satisfy the evidence-chain acceptance gate, but cannot authorize a mechanism
  claim. Downstream publication/decision policy may impose a stricter robust-update requirement.
- F9-S9 subsequently adds the hidden-world K2/headline comparison and
  posterior-calibration/false-mechanism protocol. There is still no live/private passing result,
  real materials campaign, authenticated laboratory evidence, or F9 scientific exit.
- The scorer itself does not execute actions. F9-S8 consumes continue/seek decisions, while
  fork/retire/stop remain explicit non-continuation outcomes.

The next evidentiary work is a prospective custody-bound execution of the F9-S9 harness, followed by
a real materials alternatives-to-update chain and F10 registered experiment capabilities.
