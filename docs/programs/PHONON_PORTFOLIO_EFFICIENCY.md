# Phonon shadow-portfolio efficiency

This workflow derives the production endurance efficiency receipt from the observation-blind human
baseline and the single in-window shadow epoch. It does not accept operator-entered value, cost, or
improvement numbers.

## What the metric means

The metric is expected portfolio-planning efficiency, not realized scientific productivity:

- value is the count of distinct frozen research questions covered by selected candidates;
- cost is the sum of their preregistered durations, represented as duration microseconds;
- the baseline is exactly one human-selected scientific experiment candidate; and
- the endurance arm is the deterministic planner's shadow-selected batch.

The adapter maps replication/external-transfer candidates to the predictive question and mechanism
test/Campaign candidates to the mechanism question. It calculates:

~~~text
improvement_ppm = floor(
  ((planner_value / planner_duration) / (baseline_value / baseline_duration) - 1)
  * 1,000,000
)
~~~

using the equivalent integer cross-product implemented by `EnduranceEfficiencyReceipt`. A positive
result says only that the shadow plan covers more frozen questions per estimated time than the
blind one-candidate baseline. It does not say the actions ran, the expected information arrived, or
the scientific claims were confirmed. Every artifact carries
`expected_not_realized_scientific_efficiency=true` and `actions_enqueued=false`.

## Freeze the baseline before planner output

The production reviewer first commits exactly one `replication` or `mechanism_test` candidate using
the blind-plan procedure in `PHONON_ENDURANCE_PORTFOLIO.md`. Empty, multi-candidate, data-acquisition,
and Campaign-management baselines are rejected for this metric.

Before gate start and while the slate has no epoch:

~~~bash
conda run -n aletheia python scripts/run_phonon_portfolio_efficiency.py prepare \
  --portfolio-work-order artifacts/phonon-quest/endurance/portfolio/work-order-vN.json \
  --stage artifacts/phonon-quest/endurance/portfolio/stage-vN.json \
  --output artifacts/phonon-quest/endurance/efficiency/work-order-vN.json

conda run -n aletheia python scripts/run_phonon_portfolio_efficiency.py preflight-start \
  artifacts/phonon-quest/endurance/efficiency/work-order-vN.json
~~~

Preparation binds committed code, the exact portfolio work order and stage bytes, slate, human-plan
ID, one baseline candidate, all candidate-to-question mappings, every estimated duration, and the
gate's minimum improvement. The assessor identity must differ from controller, proposal, assessment,
and portfolio-evaluator roles. It computes no planner scores or selection before start.

## Derive immediately after the in-window epoch

After explicit gate start, materialize the portfolio epoch and then assess efficiency before any
Campaign transition:

~~~bash
conda run -n aletheia python scripts/run_phonon_portfolio_efficiency.py assess \
  artifacts/phonon-quest/endurance/efficiency/work-order-vN.json \
  --assessment-output artifacts/phonon-quest/endurance/efficiency/assessment.json \
  --receipt-output artifacts/phonon-quest/endurance/efficiency/receipt.json

conda run -n aletheia python scripts/run_phonon_portfolio_efficiency.py verify-assessment \
  artifacts/phonon-quest/endurance/efficiency/work-order-vN.json \
  artifacts/phonon-quest/endurance/efficiency/assessment.json
~~~

The durable epoch is independently reconstructed. Assessment rejects a blocked/empty planner batch,
any hard-filter or batch violation in the human baseline, changed candidate mappings, pre-window
epoch, non-shadow decision, or action enqueue. `assessed_at` equals the durable epoch evaluation
time because all deterministic inputs became available at that instant; retries therefore reproduce
the same receipt rather than inventing a later assessment time.

The assessment records `meets_gate_floor` but always retains the mechanically derived improvement,
including zero or negative values. Never edit or replace a below-floor receipt. The raw receipt is
passed to the separate terminal review only after the full duration and all other evidence exist:

~~~bash
conda run -n aletheia python scripts/run_endurance_gate.py finalize edg_<32-hex> \
  --efficiency artifacts/phonon-quest/endurance/efficiency/receipt.json \
  --idempotency-key endurance:finalize:<gate> \
  --principal controller:endurance
~~~

Finalization remains terminal and is not exposed by this adapter or the launchd supervisor.

## Acceptance and honest boundary

~~~bash
conda run -n aletheia pytest -q \
  tests/domains/materials/test_phonon_endurance_portfolio.py \
  tests/programs/test_endurance_gate.py
~~~

The engineering fixture proves a blind single-experiment baseline, no pre-start planner output,
exact epoch replay, independent derivation, causal database timestamps, raw receipt validation, and
an above-floor example. The production human baseline and epoch do not yet exist, so production
improvement remains unknown and may honestly block the gate.
