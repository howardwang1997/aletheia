# ADR 0053: Controller step authority boundary

- Status: accepted for the PR-7b routing boundary
- Date: 2026-08-25

## Context

PR-5 gave the durable controller a generic `ControllerStepExecutionPort`, while PR-7a added a
byte-pinned worker process boundary.  A deployment still could satisfy that port with one catch-all
callback that held proposal, compiler, execution, validation, admission, and Research Kernel
authority.  The controller service also passed only the derived plan to that callback, so an
adapter could not mechanically prove that it consumed the exact recovery projection audited for
the tick.

That shape would recreate the legacy monolithic controller behind a new interface.  It would also
make a worker-manifest or process-principal rebind indistinguishable from an ordinary adapter
configuration change.

## Decision

Pass the exact `ControllerRecoveryProjection` used to derive a tick plan through the step-execution
port.  Install one adapter for each of the eight active controller steps and no adapter for the
three passive wait/block steps.

Every active adapter has a self-derived, frozen manifest that binds:

- exactly one `ControllerStep`;
- reviewed code and configuration hashes;
- the complete, canonical authority-role set for that step;
- authority principal, policy, service manifest, and—only for signed external authorities—public
  key identity; and
- permanent false flags for catch-all callbacks, worker-held private keys, direct Kernel mutation,
  direct observation admission, and legacy optimize.

An exhaustive adapter-set manifest binds those eight manifests to the actual
`ResearchControllerManifest`, worker manifest, and worker process principal.  Construction rejects
missing or duplicate steps, a changed adapter manifest, cross-step authority rebinding, reuse of a
sensitive principal or key, and any signed scientific authority assigned to the worker or
controller-registration principal.

The executor recomputes the plan from the supplied projection before routing.  Passive waits are
implemented locally and never invoke an authority adapter.  Proposal steps may only return
`awaiting_authority`; they cannot report their own Kernel commit.  Admission may report completion
only when independent admission and its Kernel incorporation committed atomically.  Other active
steps cannot claim either authority.

## Consequences

- A production worker can no longer hide all active steps behind one generic model callback.
- The recovery evidence selected for a tick is delivered unchanged to the exact step adapter.
- The frozen deployment contract forbids placing execution, validator, admission, database, or
  Kernel signing private keys in the worker process.
- This ADR does not implement the eight concrete services or make the worker deployable.  Those
  services must still materialize and persist their exact inputs, expose narrow independent
  authority endpoints, prove the declared process/key separation in deployment, and pass live
  PostgreSQL/process-kill and target-host campaigns.
