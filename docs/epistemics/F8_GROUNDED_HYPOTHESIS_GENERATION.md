# F9-S2 F8-grounded competing-hypothesis generation

## What is available

F9-S2 turns one experiment-authorized F8 research direction into an auditable candidate set and,
only when every gate passes, an immutable F9-S1 world-model snapshot.

The admission path is:

```text
authorized F8 direction
  -> exact generation request
  -> unprivileged raw candidate batch
  -> independent complete pairwise semantic ledger
  -> explicit duplicate map + grounding checks
  -> complete pairwise prediction-discrimination proof
  -> uniform-prior F9-S1 snapshot
```

The implementation is in `aletheia.epistemics.hypotheses` and is exported by
`aletheia.epistemics`. It is a pure orchestration/admission layer until the optional final persistence
call. It does not modify the existing scheduler or K2 loop.

## Trust boundary

The generator is a proposer, not an evaluator. Its frozen manifest identifies its adapter, parser,
output schema, principal, capacity, and—when model-backed—the exact instruction and model. It has no
tool authority and receives no observation. The only scientific inputs are typed claims and accepted
prior-art relations already present in the exact F8 graph.

The semantic de-duplicator has its own frozen manifest and principal. It receives only the validated
candidate batch and must judge every candidate pair. It cannot search, read observations, delete a
candidate, choose the canonical snapshot, or assign a belief.

The deterministic harness owns:

- request/F8 binding validation;
- candidate count and role requirements;
- exact-normalized duplicate detection;
- confidence, cross-role, and transitivity blocking;
- duplicate-to-canonical mapping;
- claim-graph grounding checks;
- pairwise prediction-discrimination proofs;
- disposition, blockers, stable F9 IDs, and the uniform initial prior;
- failure sanitization, archive verification, and persistence eligibility.

## Frozen objects

| Object | Purpose | Content identity |
|---|---|---|
| `HypothesisGenerationPolicy` | count/confidence/admission thresholds and harness principal | `policy_sha256` |
| `HypothesisGeneratorManifest` | proposal runtime, parser/schema, principal, tools, model identity | `manifest_sha256` |
| `HypothesisDeduplicatorManifest` | independent semantic-review runtime and normalizer | `manifest_sha256` |
| `HypothesisGenerationRequest` | exact F8 evidence, run/question, manifests, and policy | `request_sha256` |
| `HypothesisGenerationBatch` | all valid raw question/hypothesis/assumption/prediction drafts | `batch_sha256` |
| `HypothesisDeduplicationBatch` | complete exact-bound pair judgments | `batch_sha256` |
| `HypothesisGenerationCampaign` | inputs plus mechanically derived decision and snapshot | `campaign_sha256` |

All contracts are frozen and reject unknown fields. Generated hypotheses and pair judgments use
canonical local-ID order. Output schema hashes are exported as
`HYPOTHESIS_GENERATION_OUTPUT_SCHEMA_SHA256` and
`HYPOTHESIS_DEDUPLICATION_OUTPUT_SCHEMA_SHA256`; a manifest using another schema is invalid.

## Admission semantics

The final disposition is derived, not selected by the adapter:

| Disposition | Meaning | Snapshot allowed |
|---|---|---|
| `ready_for_world_model` | all semantic, grounding, alternative, and discrimination checks pass | yes |
| `blocked_generation` | generator/de-duplicator exception, malformed output, wrong binding, or future timestamp | no |
| `blocked_grounding` | wrong question kind or missing/unknown F8 grounding | no |
| `blocked_duplicate_resolution` | unresolved, low-confidence, cross-role, non-transitive, or contradictory duplicate result | no |
| `blocked_discrimination` | at least one kept pair lacks a bidirectional same-protocol outcome disagreement | no |

For `n` raw drafts, a valid semantic ledger has exactly `n(n-1)/2` judgments. An equivalent draft is
not erased. Its `DuplicateResolution` names the canonical local ID and supporting judgment hash. The
entire original generation batch remains inside the campaign.

An alternative is grounded only when it cites at least one prior-art claim connected to the
candidate by an accepted relation in the exact F8 resolution. Null and primary drafts must cite the
candidate itself. All assumption grounding must also resolve inside that graph.

A kept pair is discriminating only when predictions in both directions name one another and disagree
inside the same observable, protocol, and finite outcome space. This is a structural precondition;
F9-S3 still has to audit whether the causal interpretation is identified, and F9-S4 has to commit the
prediction before observation access.

## Running a campaign

Freeze policy and both manifests before issuing the request, then pass adapters that expose those
exact manifests:

```python
from aletheia.epistemics import (
    HypothesisGenerationDisposition,
    build_hypothesis_generation_request,
    commit_hypothesis_generation_campaign,
    persist_ready_world_model,
    run_competing_hypothesis_generation,
)

request = build_hypothesis_generation_request(
    request_id="mechanism-campaign-001",
    run_id=run_id,
    question_id=question_id,
    direction_gate=direction_gate,
    policy=policy,
    generator_manifest=generator.manifest,
    deduplicator_manifest=deduplicator.manifest,
    issued_at=issued_at,
)

campaign = await run_competing_hypothesis_generation(
    campaign_id="mechanism-campaign-001",
    direction_gate=direction_gate,
    policy=policy,
    request=request,
    generator=generator,
    deduplicator=deduplicator,
)

committed = commit_hypothesis_generation_campaign(
    archive=campaign_archive,
    campaign=campaign,
)

if campaign.disposition is HypothesisGenerationDisposition.READY:
    receipt = persist_ready_world_model(campaign)
```

Do not call persistence by extracting and bypassing the campaign snapshot. The convenience function
rejects every non-ready disposition; direct F9-S1 persistence still validates the snapshot but does
not carry the F9-S2 admission proof.

## Adapter rules

A generator adapter implements an async `generate` method and returns either a
`HypothesisGenerationBatch` or an object valid against that exact schema. A semantic adapter
implements async `compare` and returns a `HypothesisDeduplicationBatch` or schema-valid object.

Operational adapters must ensure:

- transport is the only model capability; no ambient tools or inherited chat state;
- outputs bind the request/batch and frozen manifest hashes supplied by the harness;
- completion times are timezone-aware and no later than the harness receipt time;
- IDs, grounding hashes, assumptions, predictions, and pair order are complete and canonical;
- provider text is treated as untrusted data and never promoted to a disposition directly.

The repository currently includes deterministic synthetic test adapters, not a production model
adapter or a calibrated domain prompt.

## Failure, replay, and recovery

Exceptions and invalid outputs create complete blocked campaigns. The artifact retains error class,
error-detail hash, and—when an invalid raw object exists—its opaque hash. It does not retain exception
messages or unvalidated provider text.

Archive and reload with:

```python
committed = commit_hypothesis_generation_campaign(archive=archive, campaign=campaign)
loaded = load_hypothesis_generation_campaign(archive=archive, ledger=committed.ledger)
assert loaded == campaign
```

Loading rehashes archive bytes, requires canonical JSON, reconstructs every nested contract, and
rederives every decision. Treat corruption, a changed binding, or an unresolvable semantic pair as a
hard stop. To correct a scientifically inadequate candidate set, freeze a new request/campaign;
never edit or delete the earlier campaign.

## What this does not prove

F9-S2 proves that a synthetic or real adapter output is structurally F8-grounded, non-duplicate under
the recorded review, and pairwise testable as written. It does not prove:

- that a generated mechanism is true, important, feasible, or exhaustive;
- that the F8 corpus or prior-art matcher achieved production scientific recall;
- that the semantic reviewer is calibrated on a real domain;
- causal identification or absence of unmodelled confounding;
- pre-observation commitment, measurement validity, or a calibrated likelihood;
- that uniform initial beliefs are scientifically calibrated;
- an informative experiment choice, evidence update, mechanism-claim upgrade, or F9 scientific exit.

Those are separate gates in F9-S3 through F9-S7 and must be evaluated prospectively rather than
inferred from this engineering acceptance.

F9-S3's now-implemented typed causal-contract boundary is documented in
[`CAUSAL_CONTRACT_AND_IDENTIFICATION_AUDIT.md`](CAUSAL_CONTRACT_AND_IDENTIFICATION_AUDIT.md). It adds
structural and back-door identification checks, but does not retroactively turn an F9-S2 campaign
into causal evidence.
