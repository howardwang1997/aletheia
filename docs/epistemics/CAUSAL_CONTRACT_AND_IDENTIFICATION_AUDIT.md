# F9-S3 causal contract and identification audit

## What is available

F9-S3 converts a ready F9-S2 competing-hypothesis campaign into one explicit causal contract and a
mechanically derived identification audit.

```text
ready F9-S2 campaign
  -> exact no-observation causal request
  -> unprivileged typed causal contract
  -> deterministic structure + back-door audit
  -> independent complete assumption review
  -> derived disposition, future-claim ceiling, and prediction-planning authorization
  -> content-addressed immutable campaign
```

The implementation is in `aletheia.epistemics.causal` and exported by `aletheia.epistemics`. It does
not modify K2, the scheduler, observations, or the F9-S1 database schema. The complete causal campaign
is a write-once content-addressed evidence artifact.

## Contract boundary

The request exact-binds:

- the ready F9-S2 campaign and F8 direction gate;
- the F9-S1 snapshot and question version;
- every current hypothesis ID, content hash, and null/primary/alternative role;
- the complete F8 claim set and accepted prior-art relation identities;
- the proposed evidence kind;
- author, independent reviewer, and policy manifests;
- issue time and `observation_access="none"`.

Rebinding any field fails before the author adapter is called. The author sees the exact source
campaign and typed F8 claims, but no source bytes, ambient tools, scorer, or observation.

## Typed causal objects

| Object | Required scientific content | Identity |
|---|---|---|
| `CausalVariable` | definition, roles, value kind, units, observed/latent, intervenability, measurement identity, F8 grounding | `variable_sha256` |
| `CausalEdge` | source, target, mechanism, assumption IDs, F8 grounding | `edge_sha256` |
| `LatentConfounder` | explicit latent variable, at least two affected variables, assumption and evidence | `confounder_sha256` |
| `MeasurementProcess` | construct, observed indicator, exact protocol, error model, validity assumption | `process_sha256` |
| `SelectionMechanism` | selection node, parents, rule, conditioning status, exchangeability assumption | `mechanism_sha256` |
| `IdentificationAssumption` | type, statement, failure risk, hypothesis/variable scope, frozen F8 closure | `assumption_sha256` |
| `CausalEstimand` | exposure, outcome, contrast, scale, population, strategy, adjustment set, evidence kind | `estimand_sha256` |
| `HypothesisCausalGraph` | exact hypothesis version, directed edges, latent common causes, F8 grounding | `graph_sha256` |
| `CausalContract` | shared registry, one graph per hypothesis, measurement/selection, assumptions, estimand | `contract_sha256` |

Collections are canonical and bounded. Unknown fields are rejected. One graph must cover every exact
F9-S1 hypothesis version. H0 must have no directed exposure-to-outcome path; primary and alternatives
must have one. An alternative graph must retain an accepted prior-art grounding path.

This version supports one total-effect exposure/outcome estimand. The outcome may be latent only when
an explicit process maps it to an observed measurement variable. That observable and protocol must
match an F9-S2 discriminating prediction for every active hypothesis.

## Structural blockers

The author cannot set an audit result. The harness derives and records blockers for:

- changed campaign/snapshot/question/hypothesis/evidence bindings;
- missing graph coverage or wrong hypothesis version;
- undefined variables, assumptions, hypotheses, or F8 claims;
- duplicate directed relations, self-loops, and directed cycles;
- wrong exposure/outcome roles or a latent exposure;
- an intervention design whose exposure is not directly intervenable;
- missing or invalid outcome measurement;
- missing measurement/selection assumptions;
- missing consistency, positivity, exchangeability, no-interference, temporal-order, or
  measurement-validity assumptions for any hypothesis;
- invalid adjustment variables, including latent variables and descendants of exposure;
- a causal path inside H0 or no causal path inside a mechanism graph;
- unsupported identification strategies or capacity overflow.

Structural failure returns `blocked_structure` and does not call the assumption reviewer.

## Back-door audit

The only mechanically supported identification strategy in F9-S3 is
`backdoor_adjustment`. For each hypothesis graph the harness:

1. expands latent common causes, measurement edges, and selection edges;
2. verifies a DAG and the adjustment-set restrictions;
3. removes arrows emanating from the exposure;
4. selects ancestors of exposure, outcome, and conditioned variables;
5. moralizes that ancestral graph;
6. removes the adjustment variables;
7. records whether exposure and outcome remain connected, including a shortest open-path witness.

The resulting status is precise:

| Status | Interpretation |
|---|---|
| `identified_by_backdoor` | declared adjustment set satisfies this implemented criterion |
| `open_backdoor_path` | recorded path remains open |
| `invalid_adjustment_set` | set contains an invalid/latent/descendant variable |
| `invalid_graph` | structural graph facts prevent the test |
| `selection_recoverability_unsupported` | conditioned selection needs a separate recoverability/transport proof |
| `unsupported_identification_strategy` | front-door, IV, general ID, or another unimplemented strategy was requested |

Do not interpret the latter five statuses as a proof of general non-identifiability. In particular,
back-door failure does not rule out front-door or the general ID algorithm. Current tests include
fork and collider gold cases, but not a complete do-calculus implementation.

## Independent assumption review

For a structurally valid contract, the reviewer must return one exact-bound decision per assumption
in canonical order. The review manifest must identify a principal and, for model runtimes, a model
different from the causal author and prior F9-S2 proposal/review roles. Both manifests forbid tools.

Each review carries:

- `accept`, `reject`, or `unresolved`;
- confidence;
- rationale hash;
- evidence claims contained in the assumption's already-frozen F8 closure;
- completion time no later than harness receipt.

An accepted review with confidence below policy becomes `low_confidence`, not accepted. A review
cannot change assumption prose, scope, variables, or evidence. Correction requires a new causal
campaign.

## Derived outcomes

| Disposition | Condition | Future claim ceiling | Prediction planning |
|---|---|---|---|
| `ready_identified` | every graph passes back-door and every required assumption is accepted | capped by proposed evidence kind | allowed |
| `ready_bounded` | unresolved/low-confidence assumption, open path, or unsupported selection recovery | at most association | allowed |
| `blocked_assumptions` | any required assumption is rejected | none | blocked |
| `blocked_structure` | malformed/inconsistent causal contract | none | blocked |
| `blocked_execution` | adapter exception or invalid/rebound/future output | none | blocked |

The evidence-kind ceiling is also mechanical:

- descriptive and measurement-validation designs: `descriptive_only`;
- observational association: `association_only`;
- simulation intervention: `within_model_causal_only`;
- natural experiment, controlled intervention, and independent replication:
  `causal_candidate` at most.

These are ceilings on what a later valid observation could support, not evidence that an experiment
has happened. F9-S3 never emits a confirmed or strong mechanism claim.

## Running and archiving

```python
from aletheia.epistemics import (
    build_causal_contract_request,
    commit_causal_audit_campaign,
    run_causal_identification_audit,
)

request = build_causal_contract_request(
    request_id="causal-contract-001",
    source_campaign=ready_hypothesis_campaign,
    proposed_evidence_kind=evidence_kind,
    policy=policy,
    author_manifest=author.manifest,
    reviewer_manifest=reviewer.manifest,
    issued_at=issued_at,
)

campaign = await run_causal_identification_audit(
    campaign_id="causal-contract-001",
    source_campaign=ready_hypothesis_campaign,
    policy=policy,
    request=request,
    author=author,
    reviewer=reviewer,
)

committed = commit_causal_audit_campaign(archive=archive, campaign=campaign)
```

Reload with `load_causal_audit_campaign`. Archive reads rehash bytes, require canonical JSON, validate
every nested contract, and recompute all graph audits, assumption resolutions, blockers,
disposition, ceiling, and authorization.

Adapter exceptions and malformed output retain only error class and SHA-256 identities. Raw
unvalidated model text and exception messages do not enter the campaign.

## Current limitations

The repository's F9-S3 adapters and causal content are synthetic. This implementation does not yet
provide:

- real domain causal-author or assumption-review calibration;
- multiple exposures/outcomes, longitudinal treatment, mediation estimands, or dynamic regimes;
- front-door, IV, general ID/do-calculus, SWIG, MAG/PAG, transportability, or selection-recovery
  algorithms;
- proof that accepted substantive assumptions are true;
- an executed intervention, randomization receipt, physical measurement validation, or observation;
- posterior update, posterior sensitivity analysis, or mechanism-claim acceptance;
- evidence that a real F8/F9 campaign has produced a correct causal model.

F9-S4 now provides the next pre-observation prediction/likelihood and observation-staging gate; see
[`PREOBSERVATION_PREDICTION_COMMITMENT.md`](PREOBSERVATION_PREDICTION_COMMITMENT.md). Observation
validation, posterior update, and claim acceptance remain explicit later gates.
