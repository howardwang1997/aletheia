# ADR 0041: Freeze and verify external launchd endurance supervision

Date: 2026-08-18

Status: accepted

## Context

The F11-S7 controller is intentionally run-once. Its PostgreSQL advisory lock, database clock,
stable command keys, and write-once spool make overlapping invocations safe, but none of those
properties prove that an external scheduler will continue invoking it for three days. A shell loop
inside the research process would share its failure domain; an undocumented local cron entry would
not bind the Conda runtime, controller file, interval, logs, or deployed job identity.

The scheduler must be deployable before the gate starts without accidentally starting the clock.
It must also be unable to turn elapsed time into a terminal scientific claim.

## Decision

Add a content-addressed `EnduranceSupervisorManifest` and a deterministic macOS launchd plist. The
manifest freezes:

- the exact controller/gate and controller-manifest file bytes;
- repository root and supervisor/controller/plist/log paths;
- executable hashes for Conda and the environment's Python, environment prefix/name, and Python
  version;
- launchd label/domain and the controller's polling interval; and
- literal `automatic_start=false` and `automatic_finalization=false` boundaries.

The plist invokes one `conda run -n <env> python ... cycle` operation at each interval. It has no
shell interpolation, `KeepAlive` loop, start command, or finalize command. `RunAtLoad` is safe
because a cycle against a missing gate returns `waiting_for_explicit_start` after exact deployment
verification and performs no scientific mutation. Once the operator separately starts the gate, a
cycle delegates exactly one decision to the existing advisory-locked controller.

Production preflight requires exact runtime/controller/manifest/plist bytes, a controller preflight
with zero blockers, and the expected launchd label loaded with the frozen invocation paths and
interval. The supervisor and fault-evidence code/CLI are part of the controller's committed code
matrix. Drift therefore stops ticking rather than silently running a different deployment; any
resulting cadence gap remains visible and blocks final passage.

## Consequences

- Scheduler process failure is separated from research-worker and controller-process failure.
- Loading launchd before explicit start is safe and testable.
- Conda, Python, code, controller, cadence, and log drift fail closed.
- A Conda/Python upgrade during a live gate requires incident review; silently accepting it would
  destroy the frozen execution claim.
- launchd is the commissioned macOS adapter. Other platforms need equivalent signed adapters and
  acceptance, not an unreviewed command translation.
- Loaded scheduling proves invocation continuity, not scientific success. Reproduction, negative
  result, pivot, fault, portfolio, efficiency, duration, and final review remain separate evidence.

## Rejected alternatives

### Keep one Python process alive for 72 hours

Rejected because it shares a failure domain with the controller and makes restart/cadence evidence
dependent on one process lifetime.

### Let launchd run the controller `start` command

Rejected because deployment or login would then create the scientific time boundary. Start remains
an explicit, separately audited operation after all work orders pass preflight.

### Let the supervisor finalize automatically

Rejected because duration alone is not a scientific verdict. Finalization requires a separate
efficiency receipt and explicit evidence review and remains terminal even when blocked.

### Use an unversioned shell script or crontab line

Rejected because environment resolution, quoting, paths, cadence, and deployed identity would not
be content-bound or replay-verifiable.
