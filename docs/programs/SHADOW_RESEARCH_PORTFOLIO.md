# Shadow research portfolio planning

This is the F11-S5 operator/developer guide for observation-blind proposal registration,
deterministic hard filtering, constrained batch selection, and pre-result human comparison.

## Deploy

~~~bash
conda run -n aletheia alembic upgrade head
conda run -n aletheia alembic current
~~~

Expected repository head: `20260825_0024`.

- `0019` adds slate, candidate, human-plan, epoch, and score tables;
- `0020` adds append-only, applying-command, parent binding, and deferred completeness guards.
- `0021`–`0023` add later fault/endurance evidence and the separate research-kernel authority
  store; this portfolio remains a legacy-scope shadow projection.
- `0024` adds the separate qualification-only local-execution store and does not change this legacy
  portfolio authority.

API and worker startup fail closed at any other revision. `schema_diffs(connection)` must return an
empty list after upgrade.

## Trust and data flow

~~~text
latest Quest-scoped portfolio-plan memory receipt
                    + exact Quest graph
                    + current Program budget availability
                                  │
                                  ▼
model proposal: typed action + rationale only
                                  │
                                  ▼
independent assessment: frozen evidence inputs, no final verdict
                                  │
                                  ▼
append-only slate ───────► blinded human plan (planner_output_access=none)
                                  │
                                  ▼
Decimal/integer harness: hard filters → score → constrained batch
                                  │
                                  ▼
shadow epoch + human comparison (actions_enqueued=false)
                                  │
                                  ▼
read-only readiness audit → at most eligible_for_human_activation_review
~~~

There is no production activation endpoint or store method.

## Construct a slate

Use `PortfolioActionSpec` for proposals. Do not add cost or score fields; the schema rejects extras.
Supported action types are:

- `advance_campaign`;
- `discriminating_experiment`;
- `replication`;
- `mechanism_test`;
- `acquire_data`;
- `repair_capability`;
- `start_campaign`;
- `pause_program`; and
- `stop_program`.

Campaign experiment actions target `cmp_...`. Program actions target `prg_...`.
`start_campaign` also names an existing `fam_...`. The store resolves and rechecks the Program and
family from the frozen graph rather than trusting a caller-supplied Program score scope.

Before proposal generation:

1. rebuild the Quest with `ProgramGraphStore.get_quest`;
2. compact the Quest-level task `portfolio-plan` at the current latest leaf;
3. create a `TaskContextReceipt` naming the proposal provider/model; and
4. bind its receipt ID and the current `graph_sha256` into `PortfolioProposal`.

The independent `PortfolioAssessmentBatch` must cover every candidate exactly once and must not
predate the proposal. Its manifest principal differs from the proposer; an independent-model
manifest also uses another model identity. Each assessment needs at least one evidence hash and
freezes every input the harness will use.

Policy must use mode `shadow` and the runtime selector identity:

~~~python
from aletheia.programs import (
    PORTFOLIO_SELECTOR_CODE_SHA256,
    PortfolioSelectionPolicy,
)

policy = PortfolioSelectionPolicy(
    policy_id="portfolio-policy-2026-08",
    quest_id="qst_<32-hex>",
    selector_code_sha256=PORTFOLIO_SELECTOR_CODE_SHA256,
    frozen_at=policy_frozen_at,
)
~~~

Register with an idempotent scientific command:

~~~python
from aletheia.programs import GraphCommandContext, ResearchPortfolioStore

store = ResearchPortfolioStore()
receipt = store.register_slate(
    slate_spec,
    GraphCommandContext(
        idempotency_key="portfolio:quest-42:slate-1",
        principal="controller:portfolio",
    ),
)
~~~

Registration locks the Quest, reloads the graph and current memory receipt, locks every Program
allocation while calculating spend, and rejects a race. Exact replay returns the original command
receipt even if current state has since advanced.

PostgreSQL is the authoritative workflow clock, matching graph projections and scientific-command
receipts. For API clients, use `receipt.command.committed_at` (or a later server-derived timestamp)
as the human plan's `issued_at`; do not infer ordering from a potentially skewed client clock.

## Harness calculations

### Expected information gain

Information actions provide integer-ppm priors and discrete hypothesis/outcome likelihoods. The
harness calculates:

~~~text
H(prior) = -Σ p(h) ln p(h)
p(o) = Σ p(h) p(o|h)
E[H(posterior)] = Σ p(o) H(p(h|o))
EIG = H(prior) - E[H(posterior)]
~~~

It uses `Decimal` at precision 60 and half-even conversion to integer micronats. Stored expected
posterior entropy plus stored EIG equals stored prior entropy exactly. Identical likelihoods derive
zero EIG; no proposer-provided EIG is accepted.

### Hard filters

A candidate remains in the audit ledger but is ineligible for selection when any blocker exists:

| Prefix | Meaning |
| --- | --- |
| `graph:` | Quest/Program/Campaign lifecycle is incompatible |
| `dependency:` | a scientific prerequisite is not completed |
| `budget:` | Program allocation is absent or individually insufficient |
| `data:` | required Quest/Program data role is absent |
| `capability:` | required capability identity is not available |
| `measurement:` | experiment-like action lacks validated measurement evidence |
| `duration:` | policy duration limit is exceeded |
| `risk:` / `approval:` | risk is prohibited/too high or approval is missing/expired |
| `information:` | EIG model is absent or below the frozen floor |

Blockers are unique and lexically canonical. An infeasible candidate cannot be selected regardless
of utility.

### Utility and batch constraints

All metrics and weights are integer ppm. The base microscore rewards EIG, importance, novelty,
success probability, and replication-debt reduction; it subtracts cost, duration, and risk burdens.
The greedy batch adds only a marginal diversity bonus, then uses fixed tie breaks.

The final batch enforces total/per-Program/per-family caps, one action per target,
per-correlation-tag caps, cumulative allocation cost, and the replication quota. When the quota is
unsatisfiable, disposition is `policy_blocked` and the selected set is empty. A budget projection
does not write a reservation or charge.

## Commit the human baseline before evaluation

~~~python
from aletheia.programs import HumanPortfolioPlanSpec

plan = HumanPortfolioPlanSpec(
    selected_candidate_ids=("pca_<32-hex>",),
    rationale="Human allocation before seeing the planner result.",
    planner_output_access="none",
    issued_at=issued_at,
)
store.commit_human_plan(
    slate_id=receipt.object_id,
    plan=plan,
    context=GraphCommandContext(
        idempotency_key="portfolio:quest-42:human-1",
        principal="human:reviewer",
    ),
)
~~~

There is one human plan per slate. Its principal cannot be the proposer or assessor. Unknown or
duplicate candidates are rejected. The database independently checks that every selected ID belongs
to the slate and that no planner epoch exists yet.

## Evaluate in shadow mode

~~~python
epoch_receipt = store.evaluate_slate(
    slate_id=receipt.object_id,
    context=GraphCommandContext(
        idempotency_key="portfolio:quest-42:evaluate-1",
        principal="harness:portfolio",
    ),
)
epoch = store.get_epoch(epoch_receipt.object_id)
assert epoch.decision.shadow_only
assert not epoch.decision.actions_enqueued
~~~

Evaluation refuses a changed graph, budget state, or superseded memory context. It recomputes the
epoch again inside the transaction before writing. Reads later validate every scientific-command
and keyed-event receipt, re-derive all scores from the frozen slate, and verify the epoch hash.

The comparison includes hard/batch violations in the human set, exact/set overlap, Jaccard ppm, and
utility sums. It is evidence for policy review, not proof the planner is better than a scientist.

## Readiness audit

~~~python
from aletheia.programs import PortfolioShadowAuditPolicy

audit = store.shadow_audit(
    quest_id="qst_<32-hex>",
    policy=PortfolioShadowAuditPolicy(
        minimum_epochs=20,
        minimum_mean_jaccard_ppm=600_000,
        maximum_human_hard_filter_violations=0,
        maximum_planner_empty_epochs=0,
    ),
)
~~~

Passing produces `eligible_for_human_activation_review=true` and still
`autonomous_allocation_enabled=false`. F11-S6 now supplies deterministic fault evidence; F11-S7 and
a separate signed activation design remain prerequisites.

## API

Deprecated compatibility endpoints under `/legacy/research-graph` (authenticated):

- `POST /portfolios/slates`;
- `GET /portfolios/slates/{slate_id}`;
- `GET /quests/{quest_id}/portfolios`;
- `POST /portfolios/slates/{slate_id}/human-plan`;
- `POST /portfolios/slates/{slate_id}/evaluate`;
- `GET /portfolios/epochs/{epoch_id}`; and
- `GET /quests/{quest_id}/portfolio-shadow-audit`.

Mutation principals come from the authenticated identity. Viewer policy is enforced by the shared
API access dependency.

## CLI verification

~~~bash
conda run -n aletheia python scripts/research_portfolio.py list qst_<32-hex>
conda run -n aletheia python scripts/research_portfolio.py slate psl_<32-hex>
conda run -n aletheia python scripts/research_portfolio.py epoch pep_<32-hex>
conda run -n aletheia python scripts/research_portfolio.py audit qst_<32-hex> --minimum-epochs 20
~~~

The CLI is read-only. It reuses the same reconstruction and harness replay as the API.

## Incident handling

When registration/evaluation/replay fails:

1. do not enqueue or manually translate the proposed actions;
2. preserve PostgreSQL, keyed events, graph, memory archive, and assessment evidence;
3. confirm deployed code and database are at `20260825_0024`;
4. identify whether the failure is an invariant violation or normal graph/budget/memory staleness;
5. for staleness, generate a new context, proposal, assessment, and slate—never rewrite the old one;
6. for corruption, restore a verified database/archive pair or matching code; and
7. keep the system in shadow mode.

Old epochs remain audit-readable after legitimate graph or memory changes. An old memory receipt is
not deliverable for a new proposal.

## Current boundary

This layer proves observation-blind workflow ordering, exact frozen inputs, deterministic harness
calculation, hard-filter/budget compliance, constrained shadow selection, and reconstructible
human comparison. It does not prove that likelihoods or value assessments are calibrated, that a
proposed experiment is scientifically important, that approval evidence is signed, or that
autonomous allocation is safe. See ADR 0037 and the F11-S5 implementation report. The production
phonon adapter and its stricter gate-window ordering are documented in
`PHONON_ENDURANCE_PORTFOLIO.md`; that adapter also remains shadow-only.
