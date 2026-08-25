# Phonon endurance shadow portfolio

This is the precommitted portfolio producer for the first production phonon endurance Quest. It
creates one observation-blind comparison inside the real gate window without granting the planner
allocation, graph-transition, campaign-start, data-acquisition, or outward-action authority.

## Frozen alternatives

The work order binds the exact commissioning manifest, endurance controller, independent replay
protocol, initial Quest graph, committed code, and four typed alternatives:

- run the implementation-diverse same-source replication;
- run the local-packing versus global-lattice mechanism test;
- activate the already commissioned mechanism Campaign; and
- qualify an independent phonon corpus.

The final alternative deliberately requires the absent `external_validation` data role. It remains
visible in the score ledger but must be infeasible until a separate lineage/target audit creates
that role. All likelihoods, costs, capability identities, data requirements, replication debt, and
evidence hashes are frozen before the gate starts. The model proposal contains only typed actions
and rationales; the deterministic portfolio harness derives feasibility, information gain, utility,
and the constrained batch.

## Prepare after the final code commit

Replace every `vN` with one mutually bound immutable version. Do not prepare or stage an interim
work order: the controller identity includes this producer, and staging creates the authoritative
`portfolio-plan` memory leaf for the Quest.

~~~bash
conda run -n aletheia python scripts/run_phonon_endurance_portfolio.py prepare \
  --controller artifacts/phonon-quest/endurance/controller-manifest-vN.json \
  --protocol artifacts/phonon-quest/endurance/reproduction/protocol-vN.json \
  --commissioning artifacts/phonon-quest/commissioning-manifest.json \
  --output artifacts/phonon-quest/endurance/portfolio/work-order-vN.json

conda run -n aletheia python scripts/run_phonon_endurance_portfolio.py verify \
  artifacts/phonon-quest/endurance/portfolio/work-order-vN.json
~~~

Preparation is zero-output with respect to PostgreSQL and scientific models. Verification rehashes
the code and three bound files, reconstructs their committed provenance, and requires the exact
initial Quest graph.

## Stage before gate start

~~~bash
conda run -n aletheia python scripts/run_phonon_endurance_portfolio.py stage \
  artifacts/phonon-quest/endurance/portfolio/work-order-vN.json \
  --output artifacts/phonon-quest/endurance/portfolio/stage-vN.json
~~~

Staging registers one exact Quest-scoped memory fact, its complete compaction/context receipt, and
one append-only shadow slate. It prints the candidate IDs and titles needed by the human reviewer.
It does not materialize an epoch, inspect a planner verdict, enqueue an action, change the Quest
graph, or start the endurance gate. Exact retry reconstructs the same receipt.

## Commit a genuinely human blind baseline

Before any evaluation, a person selects zero or more IDs from the staged list and writes a file
such as:

~~~json
{
  "human_choice_confirmed": true,
  "planner_output_access": "none",
  "rationale": "Human allocation written before seeing the planner result.",
  "selected_candidate_ids": ["pca_<32-hex>"]
}
~~~

The system must not generate this choice or rationale on the reviewer's behalf. Commit it with an
explicit `human:*` principal:

~~~bash
conda run -n aletheia python scripts/run_phonon_endurance_portfolio.py commit-plan \
  artifacts/phonon-quest/endurance/portfolio/work-order-vN.json \
  artifacts/phonon-quest/endurance/portfolio/stage-vN.json \
  artifacts/phonon-quest/endurance/portfolio/human-selection-vN.json \
  --human-principal human:<reviewer> \
  --output artifacts/phonon-quest/endurance/portfolio/human-plan-vN.json

conda run -n aletheia python scripts/run_phonon_endurance_portfolio.py preflight-start \
  artifacts/phonon-quest/endurance/portfolio/work-order-vN.json \
  artifacts/phonon-quest/endurance/portfolio/stage-vN.json
~~~

Preflight is ready only when the exact human plan exists, no epoch exists, the gate has not started,
and code, files, graph, memory, and slate still match. Before the human commit its sole expected
blocker is `human_plan:not_committed`.

The generic shadow ledger permits zero or more human candidates. This production run's derived
efficiency protocol is stricter: the reviewer must choose exactly one `replication` or
`mechanism_test` candidate. That constraint is checked after the blind commit without calculating
planner output; see `PHONON_PORTFOLIO_EFFICIENCY.md`.

## Materialize one in-window shadow epoch

After the separate explicit gate start, and before any Campaign graph transition, run:

~~~bash
conda run -n aletheia python scripts/run_phonon_endurance_portfolio.py evaluate \
  artifacts/phonon-quest/endurance/portfolio/work-order-vN.json \
  artifacts/phonon-quest/endurance/portfolio/stage-vN.json \
  --output artifacts/phonon-quest/endurance/portfolio/epoch-vN.json
~~~

PostgreSQL supplies the evaluation time. Evaluation fails before the gate, after a graph change,
without a human plan, or against a terminal/different gate. The resulting epoch must be inside the
gate window and carries literal `shadow_only=true`, `actions_enqueued=false`, and
`automatic_graph_transition=false`. Exact retry returns the same epoch.

The epoch is evidence for the endurance checkpoint and later policy calibration. It is not an
instruction to execute its selected batch. Scientific work and any negative-result-caused pivot use
their own typed, separately authorized workflows.

Derive the expected portfolio efficiency receipt immediately after this epoch and still before the
first graph transition. It compares frozen question coverage per estimated duration and does not
claim realized scientific efficiency.

## Incident handling

If preparation, staging, preflight, or evaluation detects drift:

1. do not start the gate or translate candidates into tasks;
2. preserve the work order, stage, human plan, database, memory archive, and controller spool;
3. identify code/file drift versus graph/memory/slate staleness;
4. never overwrite an old content-addressed artifact or append a second `portfolio-plan` fact to
   make an interim work order pass; and
5. rebuild mutually bound controller/protocol/work-order identities from a reviewed commit before
   staging the production Quest.

## Acceptance and current boundary

~~~bash
conda run -n aletheia pytest -q \
  tests/domains/materials/test_phonon_endurance_portfolio.py \
  tests/programs/test_portfolio.py
~~~

The tests prove blind-plan ordering, `human:*` enforcement, exact replay, pre-start rejection,
in-window evaluation, external-data hard filtering, and zero action authority. No production work
order has yet been staged, no human baseline has been committed, and the real 72-hour clock remains
unstarted.
