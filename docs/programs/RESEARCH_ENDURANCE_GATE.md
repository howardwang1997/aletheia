# Research endurance gate

F11-S7 supplies a restart-safe, append-only endurance ledger for one frozen Quest. The engineering
path is implemented and accelerated acceptance passes. The first real 72-hour Quest completed in
2026-08, but its authoritative report is `blocked`, not `passed`, because the precommitted structural
pivot floor was `1` and the observed count was `0`. It is ineligible for scientific-exit review.

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

The real defaults are 259,200 seconds, hourly checkpoints, and a two-hour maximum gap. Production
uses the supervised run-once controller; the low-level `start`/`checkpoint` commands remain useful
for inspection and accelerated engineering, but are not the production scheduler.

## Supervised run-once controller

Freeze the committed controller code, gate identity, five-minute supervisor cadence, and safe
write-once spool before starting:

~~~bash
conda run -n aletheia python scripts/run_endurance_controller.py prepare \
  endurance-manifest.json \
  --controller-key quest-2026q3-real-72h-controller \
  --principal controller:endurance \
  --spool-root artifacts/endurance/quest-2026q3/spool \
  --poll-seconds 300 \
  --output endurance-controller-manifest.json

conda run -n aletheia python scripts/run_endurance_controller.py preflight \
  endurance-controller-manifest.json
~~~

Preparation fails unless every controller component is tracked and committed. Preflight rehashes
the live code, reconstructs the frozen Quest/gate sources, rejects an unfinished competing gate,
and requires an empty pending/committed/receipt spool. It does not start the clock.

Only after the scientific workers and external supervisor are deployed, explicitly start once:

~~~bash
conda run -n aletheia python scripts/run_endurance_controller.py start \
  endurance-controller-manifest.json
~~~

On the commissioned macOS host, freeze and load the content-addressed launchd deployment described
in `programs/ENDURANCE_LAUNCHD_SUPERVISOR.md`. It invokes one operation every five minutes:

~~~bash
conda run -n aletheia python scripts/run_endurance_controller.py tick \
  endurance-controller-manifest.json
~~~

Every invocation obtains a gate-specific PostgreSQL advisory lock, observes
`clock_timestamp()`, reconstructs the durable tail, and either does nothing, appends the next
parent-hashed checkpoint, or recovers a spool item whose database commit preceded the process
crash. Stable checkpoint command keys derive from the frozen controller and previous ledger tail.
Lock contention is recorded and leaves the active owner in control.

The frozen launchd adapter binds exact Conda/Python executables, controller/manifest/plist bytes,
paths, logs, label, domain, and cadence. `RunAtLoad` is safe before gate start: the supervisor cycle
returns `waiting_for_explicit_start` without mutation. Final start readiness remains false until the
expected job is loaded and independently reported by launchd. The supervisor exposes neither start
nor finalize.

The spool has `pending`, `committed`, and `receipts` directories. Evidence is content-addressed and
hard-link-created without overwrite; retrying identical evidence returns the first envelope even
when the submitting process or local timestamp changed. Submit typed evidence with:

~~~bash
conda run -n aletheia python scripts/run_endurance_controller.py submit \
  endurance-controller-manifest.json checkpoint-evidence.json \
  --producer worker:phonon-reproduction

conda run -n aletheia python scripts/run_endurance_controller.py status \
  endurance-controller-manifest.json
~~~

The passing F11-S6 report sealed into the gate manifest is only a pre-start qualification; it does
not count as either required in-window interruption. During the live window, run and commit a new
Quest-scoped fault bundle with the same frozen production harness/environment. Convert its exact
committed report into content-addressed process/provider receipts without hand-written JSON:

~~~bash
conda run -n aletheia python scripts/submit_endurance_fault_evidence.py \
  endurance-controller-manifest.json \
  artifacts/fault-campaigns/in-window/evidence-bundle.json \
  --producer harness:f11s6-production

conda run -n aletheia python scripts/run_endurance_controller.py tick \
  endurance-controller-manifest.json
~~~

The adapter independently revalidates the complete bundle, requires exact replay from the
append-only fault store, selects exactly one passing API-process `process_exit` and one passing
provider `unavailable`/`timeout` scenario, and rejects observations before the database-recorded
gate start. Retrying the same bundle reuses the first evidence envelope; only the controller tick
may append it to the endurance chain.

The controller CLI has no caller-clock option and deliberately has no `finalize` command. It never
turns elapsed time or partial evidence into a terminal claim. Finalization remains the separate,
explicit review operation below. Do not pass `--accelerated-now` to a real low-level command; it
fails closed at every layer.

For the commissioned phonon Quest, the first gate-bound scientific producer is the zero-fit
implementation-diverse workflow in `programs/PHONON_IMPLEMENTATION_REPRODUCTION.md`. Its production
protocol/result commands fail before this exact gate starts, and its same-source evidence cannot be
relabeled as external replication.

The production portfolio workflow in `programs/PHONON_ENDURANCE_PORTFOLIO.md` is staged before
start, requires a genuine human-blind baseline, and materializes one shadow epoch only after start
and before any graph transition. It cannot enqueue its selected batch.

If and only if the later committed reproduction is contradicted, the conditional workflow in
`programs/PHONON_NEGATIVE_RESULT_PIVOT.md` verifies its exact negative fact and durable envelope,
then records the two causal graph transitions and submits the typed pivot. Other reproduction
outcomes do not mutate the graph merely to satisfy this gate.

## Checkpoint evidence

An evidence JSON document has three arrays:

~~~json
{
  "reproductions": [],
  "interruptions": [],
  "structural_pivots": []
}
~~~

The run-once controller appends it at the frozen cadence. The following low-level form is retained
for accelerated engineering and incident diagnosis:

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

For the production phonon Quest, do not hand-author these fields. Use the blind-epoch derivation in
`programs/PHONON_PORTFOLIO_EFFICIENCY.md`. Its question-coverage/duration result is explicitly
expected planning efficiency because the shadow actions are not executed; a below-floor result is
retained and blocks final passage.

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
2. run controller `status` plus gate `show` and verify the start/checkpoint tail;
3. invoke one controller `tick`; it reconciles pending receipt IDs against the durable chain;
4. let the stable tail-derived command key replay an ambiguous database commit; and
5. do not reset the start time or repeat an evidence receipt.

If a checkpoint is late, append it anyway. The final maximum-gap calculation will retain and block
the run. If an outward action is ambiguous, keep it `reconciliation_required`; do not retry a raw
effect merely to make the endurance metrics green.

## Acceptance

~~~bash
conda run -n aletheia pytest -q tests/programs/test_endurance_gate.py
conda run -n aletheia pytest -q tests/programs/test_endurance_controller.py
conda run -n aletheia pytest -q tests/programs/test_endurance_fault_evidence.py
conda run -n aletheia pytest -q tests/programs/test_endurance_supervisor.py
~~~

The suite covers contract anti-forgery, non-cosmetic pivots, migration/trigger parity, persistent
resume/idempotency, retained blocked finalization, real-clock override rejection, and a complete
accelerated end-to-end run. Controller coverage adds advisory-lock exclusion, safe start replay,
evidence-submit replay, scheduled and evidence-triggered checkpoints, code/spool preflight, and
recovery when the database commit wins immediately before a process crash. The accelerated run
includes two questions, three Campaigns, an exact
negative result, reproduction, process/provider faults, causal structural pivot, portfolio epoch,
and efficiency receipt, but correctly remains ineligible for real 72-hour exit.
Fault-evidence coverage additionally proves deterministic typed-receipt selection, exact committed
report replay, pre-window rejection, content-addressed submission replay, and checkpoint ingestion.
Supervisor coverage proves exact no-shell Conda invocation, unloaded-job blocking, pre-start
waiting, live run-once delegation, and deployment-plist drift rejection.

## Honest boundary

This implementation makes the 72-hour experiment executable and auditable. For v1, the 72 hours did
elapse and the terminal report was frozen, but elapsed time alone did not satisfy the scientific gate.
The report does not prove that supplied scientific measurements are true, that its reproduction is
independent enough for F12, or that portfolio allocation may be activated. Those require a qualifying
precommitted gate, reality-linked independent replication, and separate signed/IAM authorization.
