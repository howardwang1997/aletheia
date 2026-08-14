# ADR 0018: Explicit causal contracts and bounded identification claims

- Status: Accepted
- Date: 2026-08-15
- Scope: F9-S3 / Competitive Causal World Model

## Context

F9-S2 guarantees that admitted hypotheses are F8-grounded, semantically non-duplicate under a
recorded independent review, and pairwise disagree on an observable prediction. It does not make the
causal structure behind those predictions explicit. A prose mechanism can still hide:

- an undefined exposure, outcome, mediator, or confounder;
- a directed cycle or temporally reversed edge;
- an unmeasured endpoint presented as observed;
- a latent common cause omitted from the adjustment argument;
- conditioning on selection or a collider;
- an invalid adjustment set containing a descendant of exposure;
- unsupported consistency, positivity, exchangeability, interference, or measurement assumptions;
- a general claim of “identified” when only one sufficient criterion was checked.

Pearl's causal-diagram work makes causal assumptions explicit and gives a graphical back-door
criterion: an adjustment set must contain no descendant of the exposure and must block every path
entering it through the back door:
[Pearl, 1995](https://proceedings.mlr.press/r0/pearl95a/pearl95a.pdf). Modern adjustment-set work
provides sound and complete criteria across richer graph classes:
[Perković et al., 2018](https://jmlr.csail.mit.edu/papers/v18/16-319.html). More generally, causal
effects can sometimes be identified even when back-door adjustment fails; the ID algorithm and
do-calculus have a broader completeness result:
[Shpitser and Pearl, 2006](https://aaai.org/Papers/AAAI/2006/AAAI06-191.pdf).

The substantive assumptions remain at least as important as graph manipulation. Consistency,
exchangeability, positivity, measurement quality, and model specification require empirical and
domain judgment rather than syntax alone:
[Hernán and Robins, *Causal Inference: What If*](https://miguelhernan.org/whatifbook).

These sources justify a mixed boundary: the harness can prove graph properties and one exact
identification criterion, while an independent reviewer must adjudicate the frozen substantive
assumptions. Neither is allowed to promote a planned design into an observed causal result.

## Decision

### Bind one causal request to one ready F9-S2 campaign

`CausalContractRequest` commits the exact F9-S2 campaign, F8 direction gate, F9-S1 snapshot,
question version, ordered hypothesis ID/version/role bindings, F8 claim and prior-art relation sets,
proposed evidence kind, manifests, policy, and issue time. It explicitly has
`observation_access="none"`.

A blocked F9-S2 campaign cannot issue the request. Any campaign, snapshot, hypothesis, or evidence
rebinding fails before the causal author runs.

### Represent causal content as a shared typed contract

A `CausalContract` contains:

- a canonical shared variable registry with definition, role, value kind, units, observability,
  intervenability, observable/protocol identity, and F8 grounding;
- one exact-bound `HypothesisCausalGraph` for every admitted hypothesis;
- explicit directed causal edges with mechanism, assumptions, and F8 grounding;
- explicit latent-confounder hyperedges;
- explicit construct-to-indicator measurement processes and error-model identities;
- explicit selection mechanisms and whether analysis conditions on selection;
- one exposure, outcome, contrast, effect scale, population, proposed evidence kind,
  identification strategy, and adjustment set;
- frozen identification assumptions and their hypothesis/variable/evidence scope.

The current slice supports one total-effect exposure/outcome estimand and requires an explicit
outcome measurement process. Its observed indicator and protocol must match a discriminating F9-S2
prediction for every active hypothesis. H0 cannot contain a directed exposure-to-outcome path;
primary and alternative mechanisms must contain one. Alternative graphs retain accepted F8 prior-art
grounding.

### Make structural audit deterministic

The harness, not the author or reviewer, checks:

- exact F9/F8 bindings and complete graph coverage;
- undefined variable, assumption, hypothesis, and grounding references;
- duplicate directed relations and directed cycles, with a cycle witness;
- exposure/outcome role, observability, and intervention compatibility;
- measurement and selection-process closure;
- required assumption kinds for every hypothesis;
- null-versus-mechanism path semantics;
- adjustment observability, exposure/outcome exclusion, and descendant exclusion;
- resource bounds over variables, assumptions, and expanded graph relations.

Every graph receives a `HypothesisGraphAudit` containing its exact hash, directed causal-path witness,
adjustment set, back-door status, and any open path witness.

### Support only the exact back-door criterion in this slice

For a structurally valid DAG, the harness expands declared latent common causes, measurement links,
and selection links. It removes arrows emanating from the exposure, constructs the ancestral moral
graph of exposure/outcome/conditioned variables, removes the adjustment set, and checks graph
separation. Gold tests cover observed and latent forks plus collider opening under conditioning.

The status is explicitly one of:

- `identified_by_backdoor`;
- `open_backdoor_path`;
- `invalid_adjustment_set`;
- `invalid_graph`;
- `selection_recoverability_unsupported`;
- `unsupported_identification_strategy`.

Back-door failure is not called “generally non-identifiable.” Front-door, instrumental-variable,
general ID/do-calculus, MAG/PAG adjustment, and selection recoverability remain unsupported and
cannot receive an identified status. Conditioning on selection is bounded rather than treated as
ordinary covariate adjustment.

### Separate assumption review from graph authorship

The causal author and assumption reviewer have separate frozen principals, manifests, parsers,
schemas, and—when model-backed—model identities. The reviewer must also differ from F9-S2's proposal
and semantic-review roles. Neither role receives tools or observations.

The reviewer must adjudicate every exact assumption once as `accept`, `reject`, or `unresolved`, with
confidence, rationale hash, and evidence limited to that assumption's frozen F8 evidence closure.
Low-confidence acceptance is mechanically treated as unresolved. The reviewer cannot edit an
assumption, graph, estimand, or evidence set; revision requires a new campaign.

### Derive disposition and claim ceiling conservatively

- Structural errors produce `blocked_structure`; no reviewer is called.
- A rejected required assumption produces `blocked_assumptions`, no claim ceiling, and no
  prediction-planning authorization.
- Unresolved/low-confidence assumptions, open back-door paths, or unsupported selection recovery
  produce `ready_bounded`; prediction planning may continue, but the ceiling is at most association.
- Complete accepted assumptions plus back-door identification produce `ready_identified`.

Even `ready_identified` means only “identified by this graph and criterion under reviewed
assumptions.” The proposed evidence kind further caps what a later valid observation could support:
descriptive/measurement designs remain descriptive, observational association remains
associational, simulation remains causal only within the frozen model, and natural/controlled/
replication designs reach only `causal_candidate`. F9-S3 never produces a confirmed or strong causal
claim.

### Archive all decisions and sanitize failures

Adapter exceptions, malformed output, wrong bindings, incomplete review, and future timestamps
produce blocked execution artifacts. Error and invalid-output detail is hash-only. Complete
campaigns can be stored in the existing write-once content-addressed archive; load revalidates
canonical bytes and mechanically rederives every audit, resolution, disposition, and ceiling.

## Consequences

- A causal story becomes an inspectable evidence artifact instead of hidden prompt prose.
- Each competing hypothesis must explain the same typed exposure, outcome, measurement, and
  estimand boundary.
- Open back-door and causal-path witnesses make failures actionable for experiment design.
- Scientific judgment is explicit but cannot override graph facts or silently expand evidence.
- The system can continue planning a discriminating intervention under bounded identification while
  preventing a mechanism-claim upgrade.
- Contracts are larger and complete review scales with the number of assumptions.
- Conservative single-estimand/back-door scope blocks some valid advanced analyses until their own
  verified algorithms and tests are added.
- Passing F9-S3 is not evidence that assumptions are true, the intervention was executed, the
  measurement worked, or a causal effect exists.

## Rejected alternatives

- **Store a DAG image or prompt transcript:** neither supports exact reference, graph algorithms,
  version validation, or replay.
- **Let the LLM report “identified”:** it can omit a confounder, condition on a bad control, or invoke
  a theorem that was not implemented.
- **Call every back-door failure non-identifiable:** front-door or general ID may still identify the
  effect.
- **Treat accepted assumptions as empirical facts:** reviewer judgment only authorizes a bounded
  identification argument; later observations and sensitivity checks remain required.
- **Ignore measurement and selection nodes:** graph separation can be valid for the wrong endpoint or
  selected population.
- **Allow one causal graph for only the primary hypothesis:** alternatives would not face the same
  estimand and measurement boundary.
- **Use proposed evidence tier as achieved evidence:** a planned controlled intervention is not an
  executed, valid, or replicated observation.
- **Implement the full ID algorithm without gold cases and witnesses:** broader claims would outrun
  the tested mathematical surface.
