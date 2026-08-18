# Endurance launchd supervisor

This is the commissioned macOS external scheduler for the F11-S7 run-once controller. It can wait
or tick; it cannot start or finalize a gate.

## Freeze deployment

Prepare only after the final pre-start code commit and the current controller manifest exist:

~~~bash
conda run -n aletheia python scripts/run_endurance_supervisor.py prepare \
  artifacts/phonon-quest/endurance/controller-manifest-vN.json \
  --supervisor-key phonon-quest-real-72h-launchd \
  --launchd-label org.aletheia.phonon-endurance \
  --conda-executable /Users/howardwang/miniconda3/bin/conda \
  --conda-environment aletheia \
  --manifest-output artifacts/phonon-quest/endurance/supervisor/manifest.json \
  --plist-output artifacts/phonon-quest/endurance/supervisor/org.aletheia.phonon-endurance.plist \
  --stdout-log artifacts/phonon-quest/endurance/supervisor/stdout.log \
  --stderr-log artifacts/phonon-quest/endurance/supervisor/stderr.log
~~~

Replace `vN` with the immutable controller version generated from that final commit; never repoint
an older manifest in place.

Preparation is write-once and replay-safe. It hashes the exact Conda and environment Python
executables and binds the repository, controller file, gate, five-minute interval, plist, and log
paths. It does not contact launchd or start the gate.

## Load before gate start

Load the exact generated plist into the current user's launchd domain:

~~~bash
launchctl bootstrap gui/501 \
  artifacts/phonon-quest/endurance/supervisor/org.aletheia.phonon-endurance.plist

conda run -n aletheia python scripts/run_endurance_supervisor.py preflight \
  artifacts/phonon-quest/endurance/supervisor/manifest.json
~~~

`preflight` fails until the job is loaded. It also rehashes the runtime, controller code and file,
manifest, and plist; re-runs controller start preflight; and verifies that launchd reports the
frozen invocation paths and interval. A zero-blocker report is the supervisor portion of final
start readiness.

Because the plist uses `RunAtLoad`, its first invocation happens immediately. Before explicit gate
start the expected result is `waiting_for_explicit_start`, with zero checkpoints and no pending
evidence. The cycle command is safe for an independent smoke check:

~~~bash
conda run -n aletheia python scripts/run_endurance_supervisor.py cycle \
  artifacts/phonon-quest/endurance/supervisor/manifest.json
~~~

Only after every scientific/fault/portfolio work order is frozen and all final preflights pass may
an operator use the separate controller `start` command. Subsequent launchd cycles invoke one
advisory-locked controller tick; overlaps return safely, and no cycle finalizes the run.

## Incident handling

Inspect the job and controller without changing the gate:

~~~bash
launchctl print gui/501/org.aletheia.phonon-endurance
conda run -n aletheia python scripts/run_endurance_controller.py status \
  artifacts/phonon-quest/endurance/controller-manifest-vN.json
~~~

If runtime, code, manifest, or plist bytes drift, cycles fail closed and the error is retained in
the frozen stderr log. Restore the exact committed deployment and invoke one cycle. Do not reset the
gate start or erase a late checkpoint; the final gap calculation must preserve the incident.

To remove the scheduler after a terminal report or an abandoned pre-start deployment:

~~~bash
launchctl bootout gui/501/org.aletheia.phonon-endurance
~~~

Bootout changes only scheduler state. It never deletes manifests, logs, spools, checkpoints, or
reports.

## Acceptance

~~~bash
conda run -n aletheia pytest -q tests/programs/test_endurance_supervisor.py
~~~

The tests prove exact no-shell Conda invocation, unloaded-job blocking, pre-start waiting, live
run-once delegation, absence of automatic start/finalization, and plist-drift rejection.
