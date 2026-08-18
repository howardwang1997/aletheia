# Research endurance gate

F11-S7 supplies a restart-safe, append-only endurance ledger for one frozen Quest. The engineering
path is implemented and accelerated acceptance passes. No real 72-hour Quest has completed yet.

## What is frozen

A prepared manifest requires and seals:

- one active Quest and its exact specification hash;
- at least two bound, content-verified research-question versions;
- at least three Campaign branches;
- at least one budget allocation and the complete budget manifest;
- the complete data-role manifest;
- the latest passing Quest-scoped F11-S6 fault campaign/report;
- harness code and environment hashes; and
- duration, checkpoint interval/gap, evidence floors, and minimum 10% efficiency improvement.

Campaign transitions remain allowed. Quest direction, frozen questions, budget authority, and data
roles do not. This allows a real negative-result pivot without allowing the run to rewrite its
mission or expand its authority.

## Evidence classes

| Class | Clock | Maximum claim |
| --- | --- | --- |
| `accelerated_engineering` | explicit test clock or database clock | contract/recovery acceptance only |
| `real_time_72h` | PostgreSQL `clock_timestamp()` only | F11 scientific-exit review after ≥259,200 seconds |

The real class rejects `--accelerated-now` at the API, store, model, trigger, and report levels. A
passing accelerated report always has `real_72h_passed=false`.

## Durable lifecycle

The schema head is `20260818_0022`:

~~~bash
conda run -n aletheia alembic upgrade head
conda run -n aletheia alembic current
~~~

The three tables are append-only:

1. `research_endurance_gates`: manifest and database-observed start;
2. `research_endurance_checkpoints`: sequence, parent SHA-256, ledger observation, and new evidence;
3. `research_endurance_reports`: one terminal pass/blocked/failed report.

Each mutation shares a transaction with a `research_endurance.mutation` receipt and keyed event.
There is no mutable `running` row. Resume reconstructs the unique last checkpoint and appends
`sequence + 1`; replaying the same idempotency key returns the first receipt.

## Prepare and start a real gate

First run and commit a current, passing F11-S6 campaign. Then prepare the manifest:

~~~bash
conda run -n aletheia python scripts/run_endurance_gate.py prepare qst_<32-hex> \
  --gate-key quest-2026q3-real-72h \
  --fault-campaign-id fic_<32-hex> \
  --evidence-class real_time_72h \
  --harness-code-sha256 <64-hex> \
  --environment-manifest-sha256 <64-hex> \
  --output endurance-manifest.json
~~~

The real defaults are 259,200 seconds, hourly checkpoints, and a two-hour maximum gap. Review the
manifest, deploy the controller/worker, then commit the start:

~~~bash
conda run -n aletheia python scripts/run_endurance_gate.py start \
  endurance-manifest.json \
  --idempotency-key endurance:start:<stable-id> \
  --principal controller:endurance
~~~

Do not pass `--accelerated-now` to a real manifest; it fails closed.

## Checkpoint evidence

An evidence JSON document has three arrays:

~~~json
{
  "reproductions": [],
  "interruptions": [],
  "structural_pivots": []
}
~~~

Append it at the frozen cadence:

~~~bash
conda run -n aletheia python scripts/run_endurance_gate.py checkpoint edg_<32-hex> \
  --evidence checkpoint-evidence.json \
  --idempotency-key endurance:checkpoint:<gate>:<sequence> \
  --principal controller:endurance
~~~

The controller does not supply counts. The store reconstructs the Quest graph, exact in-window
negative-result facts, portfolio epochs, budget charges, one-time action/receipt state, and source
fault reports. It derives six zero-loss/duplicate/mismatch metrics in every observation.

Evidence rules:

- reproduction uses distinct original/reproduction Campaigns and an in-window validation receipt;
- process interruption resolves to a passing API/worker `process_exit` fault scenario;
- provider interruption resolves to a passing provider `unavailable`/`timeout` scenario;
- pivot resolves to an in-window negative-result fact followed by a stopped/paused/failed source
  transition and active successor transition;
- pivot fingerprints change at least two dimensions, including predictions or discriminated pairs;
- receipt IDs are content-derived and may occur only once in the checkpoint chain.

## Finalization

The efficiency receipt compares value per cost against a single-Campaign baseline. It records
either information-gain or question-coverage units, costs, an integer-derived improvement in ppm,
source hashes, and an independent assessor.

~~~bash
conda run -n aletheia python scripts/run_endurance_gate.py finalize edg_<32-hex> \
  --efficiency efficiency.json \
  --idempotency-key endurance:finalize:<gate> \
  --principal controller:endurance
~~~

Finalization is terminal even when premature. The retained report blocks/fails on any missing
duration, cadence, negative result, reproduction, process/provider interruption, structural pivot,
portfolio epoch, efficiency improvement, initial Campaign, zero invariant, or frozen boundary.
Start a new gate after remediation; never edit or delete the failed evidence.

Inspect and audit:

~~~bash
conda run -n aletheia python scripts/run_endurance_gate.py show edg_<32-hex>
conda run -n aletheia python scripts/run_endurance_gate.py list --quest-id qst_<32-hex>
conda run -n aletheia python scripts/run_endurance_gate.py audit qst_<32-hex>
~~~

Only the latest passing real-time report is eligible for F11 scientific-exit review. Audit never
enables autonomous allocation.

## Recovery and incident handling

After API/controller death:

1. deploy the same frozen code/environment identity;
2. run `show` and verify the start plus checkpoint chain;
3. reuse the exact pending idempotency key if its commit outcome was unknown;
4. otherwise append the next sequence from a new process; and
5. do not reset the start time or repeat an evidence receipt.

If a checkpoint is late, append it anyway. The final maximum-gap calculation will retain and block
the run. If an outward action is ambiguous, keep it `reconciliation_required`; do not retry a raw
effect merely to make the endurance metrics green.

## Acceptance

~~~bash
conda run -n aletheia pytest -q tests/programs/test_endurance_gate.py
~~~

The suite covers contract anti-forgery, non-cosmetic pivots, migration/trigger parity, persistent
resume/idempotency, retained blocked finalization, real-clock override rejection, and a complete
accelerated end-to-end run. The accelerated run includes two questions, three Campaigns, an exact
negative result, reproduction, process/provider faults, causal structural pivot, portfolio epoch,
and efficiency receipt, but correctly remains ineligible for real 72-hour exit.

## Honest boundary

This implementation makes the 72-hour experiment executable and auditable. It does not claim that
the 72 hours have elapsed, that supplied scientific measurements are true, that a reproduction is
independent enough for F12, or that portfolio allocation may be activated. Those require the actual
production run, reality-linked independent replication, and separate signed/IAM authorization.
