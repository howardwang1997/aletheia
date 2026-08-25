# ADR 0031: Fail-closed composition of mechanistic campaigns

Date: 2026-08-16
Status: Accepted for the engineering template; scientific execution remains blocked

## Context

F10-S6 must connect an F8 research direction, F9 competing explanations and causal/prediction
contracts, and F10 experiment capabilities into one mechanism-discriminating campaign. Merely
placing two result files beside one another is unsafe. It can hide post-outcome prediction,
unregistered executors, reused datasets or implementations, invalid observations, role reuse,
pseudoreplication, and a claim stronger than either the causal design or the capability permits.

The current materials registry contains two latest capabilities: band-gap range compression and
ASE/EMT reference simulation. Both are provisional and exploratory. The repository also has no
production F8 direction gate, ready production F9 hypothesis/causal campaign, or reserved fresh
independent confirmation. Synthetic fixtures demonstrate the preceding contracts, but they are not
scientific evidence and cannot fill those release inputs.

## Decision

1. Represent F10-S6 as an immutable `MechanisticCampaignProtocol`. It embeds the exact F8→F9
   causal campaign, at least two unique probabilistic F9 prediction campaigns, one experiment slot
   per prediction campaign, a frozen decision policy, and a frozen capability-registry snapshot.
2. Require every slot's exact capability manifest to occur in that registry snapshot. A manifest
   cannot cross a release gate merely by declaring its own lifecycle `registered`.
3. Do not infer a C1–C4 family from a generic action enum. Require a frozen
   `MechanisticCapabilityQualification` that exact-binds the manifest, family, compatible action,
   evidence hash, and a capability-role-independent domain reviewer. Then require at least two
   distinct qualified families, including C3 structural intervention or C4 simulation. Reusing a
   prediction campaign or experiment namespace across slots is rejected.
4. Separate execution authorization from scientific release eligibility. Provisional capabilities
   may run exploratory work when the F8/F9 spine is ready, but they always leave release blockers.
   Release also requires registered confirmatory capability evidence, a mechanism-capable declared
   claim type, and a sufficient F9 causal ceiling.
5. Bind each slot to the capability executor implementation hash, exact input identity, data
   identity, cost/wall-time ceilings, and a pre-observation timestamp. The campaign evaluator must
   be principal-distinct from F9 authors/reviewers/evaluators, every capability role, and fresh-data
   custody.
6. Require a `FreshConfirmationReservation` for each fresh-confirmation slot. The reservation is
   frozen before source access and binds data, implementation, custody, and one declared
   independence kind: fresh dataset, independent implementation, or external site. The selected
   identity must differ from the internal slot on the dimension that makes it independent.
7. Admit results only through the existing typed capability pipeline: content-addressed raw run,
   parser candidate, independent domain-validation report, harness validation, and committed
   ledger. Raw execution must start after protocol freeze and match the exact protocol hash, slot
   input hash, capability manifest, and measurement purpose.
8. Freeze a separate outcome-mapping implementation before source opening. The mapper may only map
   the validated observation to an outcome bin already frozen in the corresponding F9 prediction
   campaign and cannot reuse the campaign evaluator or a scientific/capability role.
9. Score each slot independently under the nominal likelihoods and every preregistered sensitivity
   scenario. A slot discriminates only when the same unique hypothesis wins every scenario and the
   minimum first-versus-second probability margin meets policy. Low margin, ties, or sensitivity
   winner changes are valid but inconclusive evidence; broken lineage or validation is invalid
   evidence.
10. Do not multiply likelihoods or compute a joint posterior across slots. The campaign decision
    uses concordance of per-slot robust winners, preventing correlated executions from creating
    artificial certainty. Conflicting winners remain explicit.
11. Intersect the final claim with both F9's causal ceiling and the registered capability claim
    types. Provisional/non-confirmatory evidence is capped at a descriptive pattern. Within-model
    capability support cannot become an experimental mechanism claim merely because the upstream
    causal contract has a higher ceiling.
12. Store assessments and the campaign decision inside a fully rederived evidence bundle. Pydantic
    validation recomputes every slot assessment and final disposition, so changing a winner,
    blocker, claim ceiling, or disposition invalidates the bundle.
13. Publish a separate `MechanisticCampaignReadinessAudit` for current repository state. It reports
    engineering-template availability independently from execution and scientific-release
    readiness and lists every missing input instead of fabricating a campaign.

## Rejected alternatives

- **Call every computational experiment C2 and every simulation C4.** An action enum does not prove
  that a capability implements the family-specific controls or is fit for a particular campaign.
  The current range-compression and ASE/EMT manifests have no F10-S6 family qualification; both are
  also provisional, and neither result was generated for one common precommitted materials question.
- **Treat a synthetic F8/F9 fixture as the selected research question.** Fixtures test software
  invariants; they have no production custody, novelty decision, or prospective scientific input.
- **Trust lifecycle text without a registry snapshot.** A locally fabricated manifest could then
  self-promote and authorize a strong claim.
- **Pool repeated likelihoods into one posterior.** Shared data, code, priors, or validators make
  independence difficult to establish; multiplication would reward pseudoreplication.
- **Classify low discrimination as invalid evidence.** A valid null, tie, or small-margin outcome is
  scientifically informative and must remain distinct from corrupt lineage or failed validation.
- **Let a positive result erase release blockers.** Observation content cannot repair missing
  registration, independence, chronology, or claim authority.

## Consequences

- Aletheia now has a reusable, tamper-evident engineering path for composing F8, F9, and F10 into a
  two-family mechanistic campaign without post-outcome prediction or joint-posterior inflation.
- The same contract preserves negative, conflicting, and inconclusive outcomes and can execute
  provisional exploratory campaigns while preventing mechanism release.
- Current state remains honestly blocked. Audit
  `materials-f10s6-current-readiness-v1` has
  `execution_ready=false`, `scientific_release_ready=false`, and `family=null` for both latest
  manifests because no independent family-qualification artifact exists.
- The registry snapshot is content-addressed but not yet backed by the signed authorization and
  promotion trust root planned for F10-S7. Reservation attestations are hashes, not external
  signatures. A real campaign must add authenticated custody and independent domain review.
- The generic campaign layer verifies identity and lineage; domain adapters must still prove that
  a data identity actually denotes the stated dataset/sample and that a mapping implementation
  correctly converts measurements to preregistered bins.
