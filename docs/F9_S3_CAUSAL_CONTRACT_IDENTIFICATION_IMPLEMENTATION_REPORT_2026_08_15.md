# F9-S3 causal contract and identification audit implementation report

- Date: 2026-08-15
- Scope: typed causal variables/graphs, measurement and selection processes, conservative back-door
  audit, independent identification-assumption review, and bounded claim ceilings
- Engineering status: complete
- Scientific-exit status: not complete

## Outcome

F9-S3 now turns an exact ready F9-S2 campaign into a causal artifact that can be inspected and
replayed without relying on prompt prose. Every admitted hypothesis gets an exact-bound graph over a
shared typed variable registry and estimand. Latent common causes, outcome measurement, selection,
directed mechanism edges, and identification assumptions are first-class content-addressed objects.

The harness rejects undefined variables, graph cycles, invalid adjustment sets, hypothesis-role/path
contradictions, unbound endpoints, missing assumptions, and F8/F9 evidence rebinding. It implements
one precise criterion—Pearl's back-door criterion—and records a causal-path or open-back-door-path
witness per hypothesis. It explicitly refuses to relabel unsupported front-door, IV, general ID, or
selection-recovery strategies as identified.

A separate reviewer must adjudicate every frozen substantive assumption. Unresolved or
low-confidence judgments and open paths permit only bounded experiment planning; rejected
assumptions block it. Even complete back-door identification yields only a future claim ceiling
appropriate to the proposed evidence kind, never an observed causal result.

This is an engineering result. The graphs, variables, reviews, and evidence in the fixture are
synthetic. No real mechanism, causal effect, or identification assumption has been validated.

## Research basis

- [Pearl 1995](https://proceedings.mlr.press/r0/pearl95a/pearl95a.pdf) supplies the back-door criterion
  and graph-separation basis used by the harness.
- [Perković et al. 2018](https://jmlr.csail.mit.edu/papers/v18/16-319.html) demonstrates why complete
  adjustment criteria and explicit graph classes matter beyond ad hoc covariate choice.
- [Shpitser and Pearl 2006](https://aaai.org/Papers/AAAI/2006/AAAI06-191.pdf) proves a broader
  identification result, motivating the explicit rule that back-door failure is not general
  non-identifiability.
- [Hernán and Robins, *Causal Inference: What If*](https://miguelhernan.org/whatifbook) motivates
  explicit consistency, exchangeability, positivity, measurement, and model assumptions whose
  credibility cannot be inferred from graph syntax alone.

ADR 0018 records how those results are narrowed to a testable Aletheia boundary.

## Delivered contracts

`aletheia/epistemics/causal.py` adds frozen, extra-forbid contracts for:

- variables, edges, latent confounders, measurement processes, and selection mechanisms;
- total-effect estimand, adjustment set, proposed evidence kind, and identification strategy;
- scoped identification assumptions;
- one exact `HypothesisCausalGraph` per F9-S1 hypothesis version;
- complete `CausalContract` and author output batch;
- complete independent assumption-review batch;
- graph audit/path witnesses and effective assumption resolutions;
- failure, disposition, claim ceiling, planning authorization, campaign, and archive receipt.

Author and reviewer output schemas have exported canonical SHA-256 identities. Their frozen
manifests contain adapter/parser/schema/principal identity and optional instruction/model identity.
Tools are forbidden. Model-backed reviewers must be different from the causal author and earlier
F9-S2 proposal/review roles.

## Exact upstream boundary

`CausalContractRequest` binds the full F9-S2 campaign hash, direction gate, world-model snapshot,
question, canonical hypothesis ID/hash/role set, F8 claims, accepted prior-art relations, proposed
evidence kind, manifests, and policy. A source campaign without a ready snapshot is rejected. Runtime
rebindings fail before adapter execution, and neither adapter receives observations.

The contract keeps F8 evidence scope explicit at variable, edge, confounder, graph, and assumption
levels. An assumption reviewer may cite only claims already inside that assumption's frozen closure;
using a different known F8 claim still fails.

## Structural and graph audit

The harness enforces:

- complete exact hypothesis coverage and canonical bounded collections;
- variable definitions, observed/latent and intervenability semantics;
- exact outcome construct-to-indicator/protocol binding to every hypothesis prediction;
- selection and measurement assumption closure;
- required consistency, positivity, exchangeability, no-interference, temporal-order, and
  measurement-validity assumptions for every hypothesis;
- simulation model-correctness assumption when applicable;
- no undefined reference, duplicate relation, self-loop, or directed cycle;
- no exposure-to-outcome path in H0 and at least one in each mechanism graph;
- observed, non-descendant adjustment variables that exclude exposure, outcome, selection, and
  measurement nodes;
- candidate grounding for H0/primary and linked accepted-prior grounding for alternatives;
- author/policy capacity over variables, assumptions, and expanded graph relations.

For supported back-door adjustment, latent-confounder hyperedges are expanded into latent parent
arrows. The harness removes exposure-outgoing arrows, builds the ancestral moral graph, removes the
adjustment set, and records a shortest remaining path. Conditioned selection is explicitly labelled
as requiring unsupported recoverability rather than treated as ordinary adjustment.

## Review and derived outcomes

The reviewer must cover every exact assumption once. `accept`, `reject`, and `unresolved` are
preserved; sub-threshold confidence becomes `low_confidence`. The campaign validator rederives every
resolution.

- all accepted plus all back-door audits identified: `ready_identified`;
- unresolved/low-confidence, an open path, or selection-recovery gap: `ready_bounded`;
- any rejected required assumption: `blocked_assumptions`;
- graph/evidence/measurement/assumption structural error: `blocked_structure`;
- adapter or output protocol failure: `blocked_execution`.

The future claim ceiling stays descriptive for descriptive/measurement designs, associational for
observational designs, within-model causal for simulation, and at most `causal_candidate` for
natural/controlled/replication designs. No state means an effect was observed or a mechanism was
confirmed.

## Failure and durability semantics

Malformed output, incomplete/reordered/rebound review, evidence-closure expansion, adapter
exceptions, and future timestamps become hash-only blocked execution artifacts. Reviewer failures
retain and revalidate the structurally valid contract and graph audits. Campaign decisions cannot be
forged through Pydantic reconstruction.

The content-addressed archive stores canonical campaign JSON. Load rehashes the bytes, validates all
nested models, and reexecutes structural/back-door/review derivation. Tampering is detected before an
artifact can be reused.

## Test evidence so far

Focused F9-S3 acceptance:

```text
38 passed in 4.54 s
changed Python Ruff and compilation: passed
```

All F9-S1/S2/S3 epistemics tests:

```text
85 passed in 8.00 s
```

Coverage includes:

- real synthetic F8-S1–S5 → F9-S2 → F9-S3 exact integration;
- adjusted observed-confounder identification with H0 versus mechanism path semantics;
- open observed and latent common-cause path witnesses;
- collider gold case: closed without conditioning and opened by collider adjustment;
- directed cycle and undefined-variable detection;
- invalid latent/descendant adjustment rejection;
- exact outcome measurement/protocol binding to all F9-S2 predictions;
- missing standard assumption and wrong hypothesis version rejection;
- unsupported general-ID strategy kept distinct from general non-identifiability;
- explicit conditioned selection bounded pending recoverability proof;
- alternative prior-art and all-object F8 grounding checks;
- independent principal/model identity and no-tool/no-observation boundaries;
- rejected, unresolved, and low-confidence assumption semantics;
- evidence-kind claim ceilings, including observational and simulation caps;
- complete review, exact assumption/evidence closure, order, timestamps, and sanitized failures;
- retained-contract failure validation, decision forgery rejection, archive round trip, and tamper
  detection.

Repository-wide acceptance:

```text
non-Docker: 1002 passed, 1 skipped, 29 deselected in 328.30 s
Docker:       29 passed, 1003 deselected in 26.27 s
```

All changed F9-S3 Python files and public exports pass Ruff and compilation. Repository-wide Ruff
still reports the same 20 pre-existing issues in out-of-scope exploratory scripts/one legacy test;
none are in the causal or epistemics implementation.

## Files added or materially changed

- `aletheia/epistemics/causal.py`;
- `aletheia/epistemics/__init__.py`;
- `tests/epistemics/f9s3_fixtures.py`;
- `tests/epistemics/test_causal_contract.py`;
- `docs/adr/0018-f9-explicit-causal-contract-and-bounded-identification.md`;
- `docs/epistemics/CAUSAL_CONTRACT_AND_IDENTIFICATION_AUDIT.md`;
- this report, README, docs index, and F7–F12 master-plan status.

## Explicit non-guarantees

- no production causal author/reviewer or real-domain calibration;
- no proof that accepted assumptions are true;
- no multiple/longitudinal exposure, mediation, dynamic-regime, or heterogeneous-effect estimand;
- no front-door, IV, general ID/do-calculus, SWIG, MAG/PAG, transport, or selection-recovery engine;
- no actual randomization, intervention, measurement validation, observation, or replication;
- no immutable prediction/likelihood receipt, posterior update, sensitivity analysis, or negative
  result revision;
- no experiment selector, K3 acceptance scorer, scheduler wiring, F9 engineering completion, or F9
  scientific exit.

## Next slice

F9-S4 should transform an authorized causal campaign into a pre-observation commitment. It must
freeze experiment/measurement identity and every active hypothesis's outcome bins or likelihood,
physically prevent observation staging before the receipt exists, reject post-observation mutation,
and probe normalization, degeneracy, calibration, and measurement uncertainty before F9-S5 can use
the predictions for EIG or explicit discrimination.
