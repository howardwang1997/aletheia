# F9-S2 competing-hypothesis generation implementation report

- Date: 2026-08-15
- Scope: exact F8 grounding, independent semantic de-duplication, pairwise discrimination, and
  mechanically derived F9-S1 snapshot admission
- Engineering status: complete
- Scientific-exit status: not complete

## Outcome

F9-S2 now converts one experiment-authorized F8 research direction into an immutable, auditable
competing-hypothesis campaign. A generator can propose H0, one primary mechanism, and alternatives,
but it cannot search outside the frozen F8 boundary, read observations, remove candidates, score its
own diversity, choose the disposition, or write a world model directly.

An independently manifested semantic reviewer must judge every candidate pair. The harness retains
all valid raw drafts, derives an explicit duplicate-to-canonical ledger, verifies exact F8 claim
grounding, and requires every kept pair to disagree bidirectionally on the same observable,
measurement protocol, and finite outcome space. Only a blocker-free campaign emits an immutable
F9-S1 snapshot; its initial belief vector is uniform and harness-authored rather than copied from
model ranking.

This is an engineering result. The included mechanisms, claims, assumptions, semantic judgments,
and measurement protocol are synthetic. It does not demonstrate real hypothesis quality, causal
identification, calibrated beliefs, or autonomous discovery.

## Research basis

- [Chamberlin's multiple working hypotheses](https://pubmed.ncbi.nlm.nih.gov/17782687/) motivates
  maintaining simultaneous alternatives rather than optimizing one favoured theory.
- [Co-Scientist](https://www.nature.com/articles/s41586-026-10644-y) demonstrates generation,
  critique, evolution, proximity/de-duplication, and experimental validation as useful components,
  while explicitly calling for broader objective evaluations beyond internal rankings.
- [Si, Yang, and Hashimoto, ICLR 2025](https://proceedings.iclr.cc/paper_files/paper/2025/file/ea94957d81b1c1caf87ef5319fa6b467-Paper-Conference.pdf)
  found stronger perceived novelty but slightly weaker feasibility for LLM ideas and identified
  self-evaluation failure and low diversity as open problems.

ADR 0017 converts those lessons into a proposal-versus-admission boundary. Model output can widen
search, but the system does not treat self-ranking, eloquence, or model confidence as scientific
evidence.

## Delivered contracts

`aletheia/epistemics/hypotheses.py` adds frozen, extra-forbid contracts for:

- F8-bound generation policy, request, generator manifest, and de-duplicator manifest;
- research-question, hypothesis, assumption, and prediction drafts;
- complete candidate and semantic pair batches;
- explicit duplicate resolutions and pairwise discrimination edges;
- sanitized stage failures and final campaign disposition;
- content-addressed committed campaigns.

Generator and de-duplicator schemas have exported canonical SHA-256 identities. Both roles forbid
tools. Model runtimes require frozen instruction/model hashes and model-only transport; deterministic
runtimes reject model transport. Distinct principals are required, and the same frozen model cannot
serve both roles.

## Exact F8 boundary

The request commits:

- the full authorized F8 direction-gate hash;
- candidate claim, corpus snapshot, claim graph, graph bundle, and prior-art resolution hashes;
- the complete input claim set and accepted prior-art relations;
- scope, run, and question identities;
- generator, de-duplicator, and policy hashes;
- issuance time and explicit absence of observation access.

Any mismatch blocks before the generator is called. The generator sees only typed claims and
accepted relations already inside this graph. Alternative hypotheses must cite an accepted linked
prior claim; H0 and primary must cite the candidate; every assumption grounding hash must resolve in
the graph.

## De-duplication and testability

For `n` raw candidates the reviewer must return exactly `n(n-1)/2` canonically ordered judgments,
each bound to both draft hashes. The harness blocks uncertain or low-confidence judgments,
exact-normalizer contradictions, equivalence across scientific roles, and inconsistent transitive
components.

Successful semantic merging records one resolution per raw draft. No raw valid hypothesis is
deleted. A duplicate names its canonical draft and the supporting judgment identity. After merging,
there must still be one null, one primary, and at least one distinct alternative.

For every kept pair, the harness constructs a discrimination edge only from predictions that:

- name one another as targets in both directions;
- share observable ID, protocol SHA-256, and exact outcome space;
- specify different expected outcomes.

One missing pair blocks the snapshot. The admitted snapshot retains only prediction drafts used by
at least one such proof, converts local IDs to deterministic stable F9 lineages, exact-binds all
members, and creates a maximum-entropy initial prior over the admitted hypotheses.

## Failure and durability semantics

Generator and semantic-review exceptions, invalid shapes, wrong hashes, incomplete pair ledgers,
and future completion timestamps become `blocked_generation` campaigns. Exceptions and unvalidated
raw outputs are represented only by hashes; their text is absent from campaign JSON. A failed
semantic review retains and revalidates the already accepted generation batch.

Campaign validation rederives duplicate maps, grounding blockers, discrimination edges,
disposition, and snapshot. A caller cannot upgrade or downgrade the result. The existing
content-addressed F8 archive stores canonical campaign JSON and detects byte, hash, or identity
tampering. Only a ready campaign can call F9-S1 persistence; exact PostgreSQL round trip remains
idempotent and immutable.

## Test evidence so far

Focused F9-S2 acceptance:

```text
26 passed in 3.31 s
changed Python Ruff and compilation: passed
```

F8 direction-gate plus F9-S1/F9-S2 integration:

```text
61 passed in 7.16 s
```

Coverage includes:

- a real synthetic F8-S1–S5 strong-novelty chain feeding F9-S2 and an exact F9-S1 snapshot;
- H0/primary/alternative closure, complete pair count, uniform prior, and PostgreSQL round trip;
- exact candidate/prior relation inputs, no observation access, and no tool authority;
- independent principals and immutable output-schema identities;
- unauthorized F8 direction and claim/hash rebinding rejection before generator execution;
- four raw drafts reduced to three only through an explicit retained duplicate mapping;
- cross-role equivalence, uncertainty, low confidence, and exact-normalizer contradiction blocking;
- unknown grounding, missing accepted prior grounding, and descriptive-question blocking;
- partial discrimination evidence retained while any missing pair blocks admission;
- incomplete/reordered pair ledger and future-timestamp rejection;
- generator/de-duplicator exception and invalid-output hash-only sanitization;
- forged dispositions/snapshots and forged retained failure batches;
- content-addressed round trip and tamper detection;
- blocked-campaign persistence rejection.

Repository-wide acceptance:

```text
non-Docker: 964 passed, 1 skipped, 29 deselected in 321.28 s
Docker:      29 passed, 965 deselected in 38.08 s
```

The first Docker matrix attempt produced 28 passes and one evaluator-owned ScienceAgentBench
candidate-container timeout at its 45-second hard limit before scoring. The exact failed isolation
case passed alone in 1.27 seconds; the complete clean rerun then passed all 29 tests. No timeout,
sandbox, or scorer policy was changed. Repository-wide Ruff still reports 20 pre-existing issues in
out-of-scope exploratory scripts/one legacy test; every F9-S2 Python file and package export passes
Ruff and compilation.

## Files added or materially changed

- `aletheia/epistemics/hypotheses.py`;
- `aletheia/epistemics/__init__.py`;
- `tests/epistemics/f9s2_fixtures.py`;
- `tests/epistemics/test_hypothesis_generation.py`;
- `docs/adr/0017-f9-f8-grounded-competing-hypothesis-admission.md`;
- `docs/epistemics/F8_GROUNDED_HYPOTHESIS_GENERATION.md`;
- this report, README, docs index, and F7–F12 master-plan status.

## Explicit non-guarantees

- no production model generator/de-duplicator, domain prompt, or real semantic calibration;
- no evidence that the synthetic F8 coverage or prior-art relations reflect real recall;
- no guarantee that the admitted set is exhaustive, true, feasible, or scientifically important;
- no causal DAG, latent-confound representation, identifiability test, or assumption reviewer;
- no immutable pre-observation prediction receipt or physical observation isolation;
- no calibrated likelihood, posterior update, negative-result revision, or sensitivity analysis;
- no EIG/cost/risk experiment selector, K3 acceptance scorer, or scheduler integration;
- no F9 engineering completion or scientific exit.

## Next slice

F9-S3 should turn each admitted mechanism into an explicit causal contract: typed variables, directed
edges, latent confounders, selection and measurement processes, estimand, intervention, and
identification assumptions. A separate reviewer must resolve those assumptions, while the harness
blocks cycles, undefined variables, unobservable endpoints, and unsupported mechanistic claim
strength.
