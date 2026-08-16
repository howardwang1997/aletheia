# F9-S6 validated-observation belief update

## What is available

F9-S6 adds a two-transaction boundary from a committed, selected experiment to an immutable child
belief state. Raw bytes stop at an independent validator; the posterior updater consumes only an
archived `validated_confirmation` artifact.

```text
committed F9-S5 selection + staged F9-S4 observation
  -> reload selection, selected prediction, and raw bytes from physical stores
  -> independent exact-payload-only validation
  -> confirmation/protocol/identity/custody/measurement/blinding/sample hard gates
  -> canonical committed validation receipt
  -> updater reloads validation archive, with no raw-observation access
  -> exact nominal posterior + every likelihood-sensitivity posterior
  -> child F9-S1 belief state and world-model snapshot
  -> retain / retire / narrow directives
  -> continue / fork hypothesis set / seek new measurement or stop
  -> open contradiction queue
  -> canonical committed update receipt
```

The implementation is in `aletheia.epistemics.belief_update` and is exported through
`aletheia.epistemics`. It validates typed evidence and derives Bayesian state transitions. It does
not run an intervention, authenticate a laboratory, independently establish construct validity,
rewrite hypotheses, persist the new snapshot into PostgreSQL, or schedule follow-up work.

## Trust and data-flow boundaries

There are two explicit transactions.

| Transaction | May receive | Must not receive | Durable output |
|---|---|---|---|
| observation validation | exact staged raw payload, frozen request, physical stores | ambient tools, unrelated observations | committed validation campaign |
| belief update | committed validated artifact, frozen policy, validation archive | raw bytes, observation-store handle | committed update campaign |

`ObservationValidatorManifest` declares `observation_access="exact_staged_payload_only"`, an empty
tool set, and a frozen deterministic or model-backed runtime. A model runtime requires an exact
instruction/model identity and model-only transport. A deterministic runtime cannot declare model
transport.

The validator principal must be distinct from the F9-S5 assessor and all F9-S2 hypothesis,
F9-S3 causal, and F9-S4 prediction/calibration principals. Model identities are separated the same
way. Both policy and manifest must have been frozen before the F9-S5 selection was committed.

The update request declares `observation_access="validated_artifact_only"`; its API has no raw
payload or observation-store parameter.

## Building a validation request

`build_observation_validation_request` accepts a committed F9-S5 campaign and one F9-S4 staging
receipt. It derives the selected candidate and exact-binds:

- selection campaign, selection receipt, and selection archive custody;
- selected prediction campaign, substantive prediction commitment, receipt, and archive custody;
- experiment namespace and protocol;
- raw-observation receipt, content hash, namespace seal, and store custody;
- validator manifest and validation policy; and
- request issue time.

The selected experiment must have been committed before `observed_at`, and staging must precede the
request. Receipt rebinding to another namespace, protocol, prediction, or commitment is rejected
when the request is built.

```python
from aletheia.epistemics import (
    build_observation_validation_request,
    commit_observation_validation_campaign,
    run_observation_validation,
)

request = build_observation_validation_request(
    validation_id="validation-001",
    committed_selection=committed_selection,
    observation_receipt=observation_receipt,
    validator_manifest=validator_manifest,
    policy=validation_policy,
    selection_archive_custody_sha256=selection_archive_custody_sha256,
    prediction_archive_custody_sha256=prediction_archive_custody_sha256,
    observation_store_custody_sha256=observation_store_custody_sha256,
    issued_at=issued_at,
)

campaign = await run_observation_validation(
    campaign_id="validation-001",
    policy=validation_policy,
    request=request,
    validator=validator,
    selection_archive=selection_archive,
    prediction_archive=prediction_archive,
    observation_store=observation_store,
)

committed_validation = commit_observation_validation_campaign(
    archive=validation_archive,
    campaign=campaign,
    committed_at=committed_at,
)
```

## Physical verification order

Before invoking the validator, `run_observation_validation`:

1. reloads the committed F9-S5 selection and requires equality with the embedded campaign;
2. identifies its one selected candidate;
3. reloads that candidate's committed F9-S4 prediction and requires exact equality; and
4. reads staged bytes through the observation receipt and rechecks the content hash.

Successful reads create separate selection, prediction, and raw-observation verification receipts.
A missing, corrupt, noncanonical, or rebound object produces `blocked_execution`; the campaign
contains no partial verifications, raw output, validation batch, or scientific probe.

## Validator output contract

`ObservationValidationBatch` must exact-bind the request and frozen scientific protocol. It records:

- selection, candidate, prediction, observation, and validator identities;
- fresh-confirmation batch and partition identities;
- outcome bin and sample count;
- confirmation/exploration/calibration role;
- experiment identity, custody, measurement validity, and blinding status;
- protocol adherence and rule-bound deviations;
- optional pre-registered small-sample rule;
- observation parser, analysis plan, measurement protocol, and measurement-error model;
- parser/analysis execution hashes, audit status, and evidence hashes; and
- completion time.

The outcome must be in the frozen F9-S4 schema. The confirmation batch and partition must be one of
the selected F9-S5 reservations and must have been available at observation time. Deviations can
reference only frozen analysis, exclusion, stopping, or parser rules. Output predating the request
or future-dated relative to the harness clock is invalid.

Malformed output and validator exceptions become hash-only failures. The raw validator payload and
exception detail are not copied into the campaign.

## Scientific admission gates

`ObservationValidationProbe` is derived from a fixed set of hard blockers.

| Boundary | Accepted condition |
|---|---|
| data role | `confirmation` |
| experiment identity | verified |
| custody chain | verified |
| measurement | valid |
| blinding | intact |
| protocol | exact or within frozen pre-registered tolerance |
| audit | `resolved_accept` |
| sample | at least policy minimum, or exact frozen small-sample rule |

`within_preregistered_tolerance` requires at least one classified, nonmaterial, in-tolerance
deviation. Material and unknown adherence block. A small-sample rule is rejected when the sample
already meets the normal minimum, preventing an unrelated exception rule from being smuggled into a
routine update.

Validation dispositions are:

| Disposition | Meaning | May build update request? |
|---|---|---:|
| `validated_confirmation` | all physical and scientific gates passed | yes |
| `rejected_scientific` | typed result is real but scientifically ineligible | no |
| `blocked_execution` | archive/store/adapter/output boundary failed | no |

## Building and running a belief update

`build_world_belief_update_request` accepts only a committed `validated_confirmation`. It derives
the question, source snapshot and belief, selection, prediction commitment, and validated outcome
identities. Policy and validation archive custody are exact-bound.

```python
from aletheia.epistemics import (
    build_world_belief_update_request,
    commit_world_belief_update_campaign,
    run_world_belief_update,
)

request = build_world_belief_update_request(
    update_id="update-001",
    committed_validation=committed_validation,
    policy=update_policy,
    validation_archive_custody_sha256=validation_archive_custody_sha256,
    issued_at=issued_at,
)

campaign = run_world_belief_update(
    campaign_id="update-001",
    policy=update_policy,
    request=request,
    validation_archive=validation_archive,
)

committed_update = commit_world_belief_update_campaign(
    archive=update_archive,
    campaign=campaign,
    committed_at=committed_at,
)
```

The updater first reloads and rehashes the validation campaign. Archive failure produces
`blocked_execution` and no posterior, snapshot, directives, or contradictions.

## Exact posterior and surprise audit

For validated outcome \(y\), the updater uses the source F9-S1 prior and selected F9-S4 likelihood:

\[
p(y)=\sum_h p(h)p(y\mid h), \qquad
p(h\mid y)=\frac{p(h)p(y\mid h)}{p(y)}.
\]

`WorldBeliefUpdateAudit` retains, for every hypothesis, prior probability, realized likelihood,
unnormalized posterior mass, normalized posterior, version, and modal-prediction match. It also
retains:

- prior and posterior entropy in nats;
- realized entropy reduction, which may be negative;
- prior-predictive probability and surprisal `-log p(y)`;
- all maximum-posterior hypotheses, including exact ties;
- whether the primary hypothesis received a negative result;
- all-model-low-likelihood and realized-outcome-uninformative flags; and
- a hash of the exact realized likelihood bundle.

Zero prior-predictive probability produces `blocked_likelihood`; the system does not invent an
epsilon or silently renormalize an impossible observation.

## Likelihood sensitivity

Every active hypothesis must expose the same set of F9-S4 sensitivity scenario IDs, and the set must
meet `minimum_sensitivity_scenarios`. For each scenario, the harness recomputes the complete
posterior, outcome predictive probability, winner set, and total-variation distance from the nominal
posterior:

\[
TV(p,q)=\frac{1}{2}\sum_h |p(h)-q(h)|.
\]

Missing/misaligned scenarios or a zero scenario predictive mass block the update as
`blocked_likelihood`. Otherwise the update is `updated_fragile` when any scenario changes the winner
or maximum TV exceeds policy; it is `updated_robust` otherwise. Fragile means the result is sensitive
to the frozen likelihood perturbations, not that the observation should be discarded.

## Immutable belief and revision semantics

A successful update creates a child `BeliefState`:

- same run, belief lineage, question, and exact hypothesis versions;
- `version = parent.version + 1`;
- exact `parent_belief_state_sha256`;
- `update_kind="validated_observation"`;
- validation commitment receipt as source observation;
- realized-likelihood bundle hash; and
- harness principal and update time.

The child is embedded in a new `WorldModelSnapshot`. The source snapshot and hypotheses remain
unchanged.

Hypothesis directives are conservative and append-only:

| Evidence state | Directive | Version consequence |
|---|---|---|
| modal prediction matched | `retain` | no new version required |
| modal miss and posterior at/below retirement ceiling in nominal plus all sensitivity cases | `retire` | new version required |
| modal miss but retirement not robust | `narrow` | new version required |

The updater does not write the narrowed scope or lifecycle transition itself. A later consumer must
materialize a new hypothesis version while preserving the source.

World directives are:

| Condition | Directive |
|---|---|
| every hypothesis assigns likelihood at/below the frozen ceiling | `fork_hypothesis_set` |
| realized likelihoods are equal within frozen tolerance | `seek_new_measurement_or_stop` |
| otherwise | `continue_current_set` |

All-model miss takes precedence over uninformative evidence.

## Contradiction queue

The update opens canonical contradiction records for:

- each hypothesis whose modal prediction missed;
- prior-predictive surprisal at or above policy threshold;
- a fragile likelihood-sensitivity result;
- all-model low likelihood; and
- an uninformative realized outcome.

Every record stays `open` and binds its hypothesis set, validation receipt, prediction commitment,
evidence hashes, reason code, severity, and detection time. The F9-S6 update does not auto-resolve
contradictions because resolution requires later evidence or scientific review.

## Replay and tamper detection

`ObservationValidationCampaign` validation recomputes the request bindings, validation probe,
blockers, and disposition. `WorldBeliefUpdateCampaign` validation recomputes posterior math,
sensitivity, fragility, snapshot, directives, contradiction queue, blockers, and disposition.

`commit_*` stores canonical JSON in a content-addressed archive and binds an explicit commit time.
`load_*` rehashes archive bytes, rejects noncanonical JSON, revalidates nested objects, and checks
object identity. Forged posteriors, directives, dispositions, or damaged bytes therefore fail
closed.

## Current limitations

- All tests use synthetic hypotheses, likelihoods, observations, validator evidence, and custody
  identities.
- The validation adapter is a trusted boundary. F9-S6 does not authenticate instruments, personnel,
  signatures, laboratory systems, or the truth of its measurement/audit assertions.
- Likelihood sensitivity is limited to the finite F9-S4 scenarios. There is no continuous robust
  Bayes neighborhood, prior sensitivity, posterior predictive check, model averaging, or unknown-
  model likelihood.
- The posterior is for discrete or pre-binned outcomes only.
- Policy thresholds are engineering defaults and are not calibrated for every scientific domain.
- Revision directives and child snapshot are artifacts; they are not transactionally persisted to
  the F9 PostgreSQL repository in this slice.
- Fresh confirmation is verified but not atomically consumed; budget, capability lease, experiment
  execution, and replication-debt transitions remain outside this module.
- There is no contradiction-resolution workflow, adaptive next-experiment loop, scheduler consumer,
  or real-domain scientific exit in this slice; F9-S7 now provides the downstream isolated K3
  acceptance scorer.
- A mathematically correct posterior cannot rescue a wrong likelihood, invalid construct, biased
  observation, misspecified causal graph, or incomplete hypothesis set.

F9-S7 now independently reopens selection/validation/update/evidence archives and checks end-to-end
temporal, discrimination, update, claim, prediction-changing revision, persistence, and stopping
evidence; see [`INDEPENDENT_K3_ACCEPTANCE.md`](INDEPENDENT_K3_ACCEPTANCE.md). The next work is the F9
scientific-exit/integration bridge: transactional child-snapshot persistence, next-round consumption,
typed scheduler projection, and frozen K3-versus-K2 hidden-world plus real-materials evaluation.
