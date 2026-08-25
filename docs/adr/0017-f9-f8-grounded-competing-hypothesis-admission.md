# ADR 0017: F8-grounded competing-hypothesis admission

- Status: Accepted
- Date: 2026-08-15
- Scope: F9-S2 / Competitive Causal World Model

## Context

F9-S1 can immutably represent a null hypothesis, primary explanation, alternatives, assumptions,
predictions, and a complete belief vector. It deliberately cannot decide whether generated
alternatives are grounded, genuinely different, or experimentally distinguishable. Accepting a
model-generated list directly would leave several failure modes open:

- the primary story can be restated as its own supposed alternative;
- a fluent but unsupported mechanism can be admitted without an F8 evidence path;
- the proposing model can delete inconvenient candidates or grade its own diversity;
- every hypothesis can predict the same observable result;
- an adapter can claim access to later observations or silently use ambient search tools;
- malformed output, provider errors, or post hoc timestamps can enter the scientific record;
- a caller can label an inadequate candidate set `ready` and persist it.

The multiple-working-hypotheses method was proposed specifically to resist attachment to one ruling
theory: [Chamberlin, 1890](https://pubmed.ncbi.nlm.nih.gov/17782687/). Modern AI systems demonstrate
that broad generation, critique, evolutionary refinement, and proximity clustering can enlarge the
hypothesis search space, but their own authors call for broader objective evaluation beyond internal
ranking: [Co-Scientist](https://www.nature.com/articles/s41586-026-10644-y). A controlled human study
found LLM-generated ideas more novel on average but slightly weaker on feasibility, while identifying
self-evaluation failure and low generation diversity as open problems:
[Si, Yang, and Hashimoto, ICLR 2025](https://proceedings.iclr.cc/paper_files/paper/2025/file/ea94957d81b1c1caf87ef5319fa6b467-Paper-Conference.pdf).

These results motivate using models as proposal engines while keeping admission, provenance, and
testability in a separate deterministic harness. They do not establish that Aletheia's generated
hypotheses are scientifically good.

## Decision

### Bind generation to one authorized F8 direction

`HypothesisGenerationRequest` commits the exact authorized `ResearchDirectionGate`, candidate
claim, corpus snapshot, claim graph and graph bundle, prior-art resolution, complete graph claim set,
accepted prior-art relations, question scope, manifests, policy, run, and question lineage.

The harness refuses generation when the F8 gate did not authorize experiments or when any binding is
changed. A request exposes `observation_access="none"`. The generator receives typed candidate and
prior-art claims plus accepted relations from that frozen graph; it receives neither source bytes,
later observations, nor a search tool.

### Separate proposal from semantic review

Generation and semantic de-duplication use different frozen manifests and principals. Model-backed
adapters must freeze their instruction and model identities and may use only model transport.
Deterministic adapters cannot declare model transport. Neither role may receive tool authority.

The generator must return exactly one null, exactly one primary, and at least one alternative in a
canonical candidate batch. Every raw valid draft is retained, including drafts later judged
duplicate. Hypotheses include rationale identity, F8 claim grounding, explicit assumptions, and
finite-outcome predictions.

### Require a complete semantic pair ledger

For `n` drafts, the independent de-duplicator must judge all `n(n-1)/2` pairs exactly once in
canonical order and bind both exact draft hashes. Its relation is `distinct`, `equivalent`, or
`uncertain`, with a confidence and rationale hash.

An exact NFKC/case-folded alphanumeric signature is a deterministic lower bound on duplication. A
reviewer cannot call an exact-normalized duplicate distinct. Uncertain or sub-threshold judgments,
equivalence across null/primary/alternative roles, and non-transitive equivalence components block
admission. Successful merging never deletes provenance: every raw draft receives a `kept` or
`duplicate` resolution and every duplicate points to a canonical raw draft through the supporting
pair-judgment hash.

### Make grounding and discrimination mechanical gates

All hypothesis and assumption grounding hashes must exist in the exact F8 graph. Null and primary
drafts must include the candidate claim. Every alternative must include a prior claim connected to
the candidate by an accepted F8 prior-art relation. Only mechanism or causal-effect questions enter
this path.

After de-duplication, at least one alternative must remain. Every pair of kept hypotheses must have
a bidirectional prediction witness with:

- the same observable identity;
- the same measurement-protocol SHA-256;
- the same finite outcome space;
- a different expected outcome;
- each prediction explicitly naming the other hypothesis as a discrimination target.

If any pair lacks a witness, the campaign is blocked. F9-S2 does not infer discrimination from prose
or from different direction labels alone.

### Derive the initial world model without model self-ranking

Only a blocker-free campaign can emit an F9-S1 `WorldModelSnapshot`. The harness derives stable
lineage IDs from the exact request and local IDs, binds every object to exact parent hashes, retains
only predictions used in discrimination proofs, and assigns a uniform maximum-entropy harness prior.
Generator or reviewer scores do not become probabilities. Later evidence updates belong to F9-S6.

The campaign disposition, duplicate mapping, discrimination edges, blockers, and snapshot are all
recomputed by the frozen Pydantic validator. Callers cannot assert or upgrade them.

### Preserve failures without retaining untrusted text

Adapter exceptions and invalid outputs produce a blocked campaign. Error detail and invalid raw
output are retained only as SHA-256 identities; raw provider text and exception messages are not
embedded. Runtime receipt checks reject output timestamps later than the harness clock.

Complete campaigns can be committed to the existing write-once content-addressed ledger. Reads
revalidate canonical JSON, object identity, and every mechanical decision. Only a `ready` campaign
can persist its exact F9-S1 snapshot.

## Consequences

- The system cannot silently turn one favourite explanation into a nominal three-way model.
- F8 evidence identity, not model memory or a fresh uncontrolled search, defines the grounding
  boundary.
- Duplicate drafts remain auditable and can be used later to evaluate generator diversity.
- Every admitted pair already names at least one concrete measurement on which it disagrees.
- Uniform initial probabilities avoid laundering internal model preference into Bayesian evidence.
- Complete pairwise review is quadratic and the current policy caps a batch at 64 candidates.
- A scientifically useful but poorly expressed alternative can be blocked; a later reviewed version
  must be a new campaign, not an in-place edit.
- Passing this gate proves structural grounding and testability, not causal identification,
  feasibility, truth, novelty, or calibration.

## Rejected alternatives

- **Keep only the top model-ranked hypotheses:** internal ranking is not empirical evidence and can
  collapse diversity.
- **Ask the generator to remove duplicates:** it can erase provenance and is not an independent
  judge.
- **Use embedding distance alone:** a threshold cannot resolve role conflicts, uncertainty, or
  transitive semantic inconsistencies and is not stable without a frozen model.
- **Accept one-sided or prose-only predictions:** the purported alternatives may still agree on all
  executable outcomes.
- **Let alternatives cite model memory:** the claim cannot be audited against the authorized F8
  knowledge boundary.
- **Use generator confidence as the initial belief:** it confuses proposal preference with evidence.
- **Search literature again inside F9-S2:** it would fork the F8 evidence boundary without coverage,
  access-right, or novelty revalidation.
- **Persist every generated snapshot:** blocked or malformed candidate sets would become usable
  scientific state.
