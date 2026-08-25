# ADR 0016: Immutable competing-world-model identities and K2 compatibility

- Status: Accepted
- Date: 2026-08-15
- Scope: F9-S1 / Competitive Causal World Model

## Context

K2 deliberately maintains one mutable `Beta(alpha, beta)` credence for the proposition that an
open-question lineage will hold on held-out data. That representation remains useful for the
existing campaign loop, but it cannot answer the questions required of a competitive causal world
model:

- Which null, primary, and alternative explanations were simultaneously live?
- Which exact wording and mechanism did an earlier experiment test?
- Which assumptions and observable predictions belonged to that exact hypothesis version?
- Did a later probability vector refer to revised hypotheses or silently overwrite old ones?
- Can historical K2 campaigns remain readable without being relabelled as richer F9 evidence?

The design follows three primary-source lessons:

- W3C PROV separates a general entity from fixed versions/specializations and defines revision as a
  directed derivation. Its constraints recommend distinct entities for versions whose attributes
  change: [PROV-DM](https://www.w3.org/TR/prov-dm/) and
  [PROV Constraints](https://www.w3.org/TR/prov-constraints/).
- Bayesian workflow is iterative and normally constructs, checks, expands, and compares multiple
  models; saving only the ultimately selected model erases scientifically relevant workflow:
  [Gelman et al., Bayesian Workflow](https://arxiv.org/abs/2011.01808).
- A causal diagram makes subject-matter assumptions explicit and permits an identification query to
  say either that available assumptions suffice or that additional observations/experiments are
  needed: [Pearl, Causal Diagrams for Empirical Research](https://escholarship.org/uc/item/6gv9n38c).

Expected information gain is a principled later experiment-selection objective, but it can be
computationally nontrivial and depends on an explicit likelihood. F9-S1 therefore stores identities
needed by later selectors without inventing likelihoods:
[Tsilifis, Ghanem, and Hajali](https://epubs.siam.org/doi/10.1137/15M1043303).

These sources motivate the data boundary. They do not show that Aletheia currently generates good
causal alternatives or calibrated probabilities.

## Decision

### Separate stable lineage identity from immutable version identity

Every new object has a typed stable identifier:

- `rq_<32 hex>` for a research-question lineage;
- `hyp_<32 hex>` for a hypothesis lineage;
- `asm_<32 hex>` for an assumption lineage;
- `pred_<32 hex>` for a prediction lineage;
- `blf_<32 hex>` for a belief-state lineage.

Each immutable payload also has a canonical content SHA-256. Version 1 has no parent. Version
`n > 1` must name the exact SHA-256 of version `n - 1`; persistence checks that the parent exists,
belongs to the same run and stable lineage, predates the child, and has not changed the
null/primary/alternative role or research-question lineage.

Changing prose while reusing the same `(stable ID, version)` is a conflict. Skipping a version,
inventing a parent, or rebinding a child to another lineage is rejected.

### Persist a closed three-way world-model snapshot

A `WorldModelSnapshot` is valid only when it contains:

- the exact `ResearchQuestion` version;
- at least three current `HypothesisVersion` objects in canonical ID order;
- exactly one null, exactly one primary, and at least one alternative hypothesis;
- at least one explicit `Assumption` for every hypothesis;
- at least one discriminating `Prediction` for every hypothesis;
- one normalized `BeliefState` over exactly those hypothesis versions;
- canonical assumption/prediction/member ordering and a freeze time after all members.

Assumptions and predictions bind both stable hypothesis ID and exact hypothesis-version SHA-256.
Predictions name a finite outcome space, expected outcome, measurement-protocol identity, and the
other hypothesis lineages they are intended to distinguish. The F9-S1 schema does not claim that
those predictions are scientifically adequate; F9-S2 and F9-S4 must generate, review, and commit
them before observations.

An F9 belief is a complete probability vector, not independent scalar confidences. Entries are
unique, canonically ordered, finite, bounded to `[0, 1]`, and sum to one within `1e-12`. A state
labelled `validated_observation` must carry both an observation receipt and likelihood-model hash;
prior or hypothesis-revision states cannot carry either. F9-S6 will own the validator and actual
update rule.

### Make every F9 row append-only

Alembic revision `20260815_0004` creates normalized question, hypothesis, assumption, prediction,
belief, belief-member, and snapshot tables. Content hashes are primary keys; stable lineage/version
pairs are unique; exact parent and member relationships are foreign-keyed. PostgreSQL triggers
reject every `UPDATE` and `DELETE`. Identical inserts are idempotent; read paths revalidate the
Pydantic payload, content hash, normalized child rows, and belief membership.

### Preserve K2 rather than pretending to migrate it

The existing `belief_states` table, `upsert_credence`, `get_credence`, `list_credences`, and all K2
events remain unchanged. The migration performs no K2 data update or backfill.

`k2_belief_state_compat` is a read-only view over the existing row. It exposes the stable projection
identity `k2::<run_id>::<question_key>`, original alpha/beta/update count, and mechanically derived
`probability_holds = alpha / (alpha + beta)`. It explicitly labels its representation
`legacy_k2_beta_bernoulli`. A rejecting `INSTEAD OF` trigger prevents writes through the view.

The projection is not an F9 `BeliefState`: it has no enumerated competing hypotheses, assumption
set, prediction set, or multi-model posterior. Automatic backfill would fabricate those missing
scientific objects and is therefore rejected.

## Consequences

- An experiment can cite the exact hypothesis/prediction version that existed before observation.
- Revision leaves every prior version and snapshot readable.
- A prose edit cannot silently inherit prior evidence under the same version number.
- A belief vector cannot drop an inconvenient alternative or rebind a probability to revised text.
- K2 campaigns remain operational and honestly labelled while F9 is introduced incrementally.
- The database contains more immutable rows and duplicated validated JSON, trading storage for
  auditability and recovery.
- F9-S1 is engineering infrastructure only. It does not yet implement semantic de-duplication,
  causal graph identification, independent assumption review, prediction receipts, observation
  validation, likelihood evaluation, EIG selection, or posterior updates.

## Rejected alternatives

- **Add alternative names to the mutable K2 row:** it still overwrites history and has no exact
  hypothesis/prediction binding.
- **Use content hashes as the only identity:** a scientific lineage could not be followed across a
  legitimate revision.
- **Keep only the latest version:** post-observation edits would become indistinguishable from
  preregistered commitments.
- **Store one probability per hypothesis independently:** the rows need not form one normalized,
  complete alternative set.
- **Backfill K2 into F9:** a binary Beta mean does not contain the missing hypotheses, assumptions,
  or predictions; constructing them retrospectively would invent evidence.
- **Let the scheduler own version semantics:** database writers, migration tools, and later services
  could bypass them. The invariant belongs in pure models, persistence checks, constraints, and
  triggers.
- **Implement EIG in F9-S1:** without reviewed likelihoods and observation validity, a selector would
  optimize invented numbers.
