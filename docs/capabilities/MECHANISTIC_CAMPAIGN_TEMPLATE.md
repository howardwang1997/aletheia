# Mechanistic campaign template

## What it does

The F10-S6 template composes one exact F8→F9 scientific lineage with two or more F10 experiment
capabilities. It freezes the question, competing explanations, causal contract, probabilistic
predictions, capability registry, experiment identities, resource ceilings, outcome mapper,
fresh-confirmation reservation, independence kind, and decision rule before observations are
opened.

It then accepts only committed typed observation pipelines and creates a fully rederived evidence
bundle. It does not execute arbitrary experiments, invent missing scientific inputs, combine
correlated likelihoods, or promote a provisional capability.

## State model

```text
production F8 gate
  -> ready F9 hypothesis campaign (H0 + primary + alternative)
  -> identified F9 causal campaign
  -> >=2 distinct probabilistic prediction campaigns
  -> frozen registry + one unique capability slot per prediction
  -> frozen campaign protocol
  -> post-freeze raw -> parse -> validate -> commit per slot
  -> pre-frozen outcome mapping
  -> independent per-slot sensitivity scoring
  -> concordance decision + immutable evidence bundle
```

The protocol has two different gates:

- `execution_authorized` asks whether the F8/F9 spine, experiment-family diversity, prediction
  commitments, and chronology are sufficient to run. A provisional capability may pass this gate
  for exploratory work.
- `mechanism_release_eligible` additionally requires registered confirmatory capabilities,
  mechanism-capable declared claim support, sufficient causal authority, and fresh independent
  confirmation. A positive observation cannot clear these blockers retrospectively.

## Experiment families

| Family | Capability action | Role in a campaign |
|---|---|---|
| C1 measurement audit | `data_audit` | check whether measurement/identity artifacts explain the pattern |
| C2 structure discrimination | `computational_experiment` | test whether aligned structure improves a frozen observable |
| C3 structural intervention | `structural_intervention` | manipulate a structural variable under explicit feasibility controls |
| C4 simulation | `simulation` | test a frozen within-model or mechanism prediction |

A campaign needs at least two distinct families, including C3 or C4. Each slot needs a distinct F9
prediction campaign and experiment namespace; duplicating one prediction across two executors is
rejected.

The action column is necessary but not sufficient. Every slot also embeds a
`MechanisticCapabilityQualification` frozen after the exact manifest and before planning. It binds
the family assignment to an evidence hash and a domain-review principal independent of all four
capability roles. Therefore a generic `computational_experiment` is not automatically C2, and a
reference calculator is not automatically qualified for a mechanism campaign.

## Evidence outcomes

| Disposition | Meaning | Maximum claim |
|---|---|---|
| `invalid_evidence` | lineage, chronology, validation, mapping, or independence contract failed | none |
| `inconclusive` | observations are valid but a robust winner/margin is absent | descriptive only |
| `conflicting_evidence` | valid slots robustly support different hypotheses | descriptive only |
| `null_supported` | every valid slot robustly supports the preregistered null | descriptive only |
| `bounded_pattern_supported` | slots agree, but a release/capability/causal gate remains | descriptive only |
| `within_model_mechanism_supported` | slots agree and capability/causal authority reaches within-model mechanism | within-model candidate |
| `mechanism_candidate_supported` | slots agree and every registration, confirmation, independence, causal, and claim gate passes | mechanism candidate |

Scoring is per slot. For the observed preregistered bin, the same unique hypothesis must have the
largest likelihood in nominal and every sensitivity scenario, with a minimum first-versus-second
margin. The campaign records concordance but fixes `joint_posterior_computed=false`; it does not
multiply potentially correlated evidence.

## Inspect current readiness

Run the audit against the current immutable materials registry:

```bash
conda run -n aletheia python scripts/mechanistic_campaign_readiness.py \
  --registry workspaces/evaluator/capabilities/materials_registry_v4.json \
  --audit-id materials-f10s6-current-readiness-v1 \
  --audited-at 2026-08-15T16:30:00Z
```

To use the audit as a CI gate, add `--require-execution-ready` or
`--require-scientific-release-ready`; a blocked result exits 2 or 3 respectively. The frozen output
is [`configs/materials/f10_mechanistic_campaign_readiness_audit_v1.json`](../../configs/materials/f10_mechanistic_campaign_readiness_audit_v1.json).

Current identities:

| Object | SHA-256 |
|---|---|
| registry v4 snapshot | `80ea6dfa5c250dbdb76a4b3b38ceb7460580d17d7cdb47695da93ff38930ad77` |
| readiness audit object | `d7fe32533ad2ea9853c35a56555d816f27b489e532a47cf6a29a10c7a89d003b` |
| readiness JSON bytes | `00ab47039424c722c3b7eaabb54f8f44985bcfe157593eddda19ff26f4f0a72c` |

The current audit is blocked by all of the following:

- no production F8 direction gate;
- no ready production F9 hypothesis or causal campaign;
- no domain-reviewed C1–C4 qualification for either latest manifest;
- fewer than two registered confirmatory experiment families;
- no registered intervention/simulation capability;
- no registered mechanism-capable claim contract;
- no fresh-confirmation reservation; and
- no independent dataset, implementation, or external-site confirmation.

This is an honest readiness result, not a failed scientific experiment. The existing
range-compression computational capability and ASE/EMT simulation capability remain useful
exploratory evidence, but neither is family-qualified for F10-S6 and they cannot be relabelled as
one prospective mechanistic campaign.

## Build a real protocol

Before source access:

1. supply a production, experiment-authorized F8 direction with a bounded materials question;
2. complete the F9 hypothesis and causal campaigns and create at least two distinct probabilistic
   prediction campaigns;
3. select exact manifests from one frozen registry snapshot and obtain independent, pre-plan
   `MechanisticCapabilityQualification` artifacts for their intended C1–C4 roles;
4. reserve the fresh source and its independent dataset/implementation/site identity;
5. create `MechanisticExperimentSlot` objects whose implementation hashes equal their capability
   executor hashes;
6. freeze a `MechanisticDecisionPolicy`; and
7. call `build_mechanistic_campaign_protocol(...)` before any source is opened.

After freeze, run each capability through `CapabilityObservationArchive`,
`parse_capability_observation`, `validate_capability_observation`, and
`commit_capability_observation_pipeline`. Freeze an independent `OutcomeMappingManifest` before
source opening, map only to an existing F9 outcome bin, then call
`evaluate_mechanistic_campaign(...)` with exactly one evidence object per frozen slot.

The full synthetic contract suite is executable with:

```bash
conda run -n aletheia pytest -q \
  tests/domains/materials/test_mechanistic_campaign.py
```

Those tests use synthetic F8/F9 gates, registered manifests, raw measurements, validation reports,
and independence attestations. They demonstrate engineering invariants only; their
`mechanism_candidate_supported` fixture is deliberately not a real materials result.

## Remaining release work

- obtain a production F8 direction and execute the F9 chain on a question whose answer was not used
  to construct the protocol;
- independently review and promote at least two relevant capability families, including C3 or C4;
- implement authenticated reservation/custody receipts and bind domain-specific dataset/sample
  identity to raw execution inputs;
- run a fresh confirmation and an independent dataset, implementation, or external site;
- obtain domain-expert review of feasibility, convergence, measurement validity, mapping, and claim
  scope; and
- retain a complete real evidence bundle even if it is negative, conflicting, or inconclusive.

The architectural rationale is in
[`ADR 0031`](../adr/0031-f10-fail-closed-mechanistic-campaign-composition.md). Current implementation
and verification details are in the
[`F10-S6 report`](../F10_S6_MECHANISTIC_CAMPAIGN_TEMPLATE_IMPLEMENTATION_REPORT_2026_08_16.md).
