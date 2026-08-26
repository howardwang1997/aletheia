# PR-7b controller step authority boundary

- Status: exhaustive worker routing and authority manifests complete; concrete production step
  services uncommissioned
- Date: 2026-08-25
- Scope: close the generic worker-callback authority gap without claiming execution, validation,
  admission, or Research Kernel signing authority

## What this slice closes

`ResearchControllerService` now passes the exact audited `ControllerRecoveryProjection` to the
step-execution port together with the deterministic plan derived from it.  The production router
revalidates all three closed contracts and recomputes the plan before any adapter call.  A stale
plan, another Quest, a changed head/snapshot, or changed blocker set fails before operational work.

`DedicatedControllerStepExecutor` requires an exhaustive deployment-frozen set of eight adapters:

| Active step | Exact authority closure | Permitted successful disposition |
|---|---|---|
| `propose_action` | action proposal | `awaiting_authority` |
| `compile_protocol` | deterministic protocol compiler | `completed` |
| `propose_redesign` | action proposal | `awaiting_authority` |
| `register_execution` | independent execution authorization | `completed` |
| `commit_validation` | DB attestation + independent validation | `completed` |
| `commit_admission` | DB attestation + independent admission + Kernel command | `completed`, with both atomic commit flags |
| `derive_continuation` | deterministic continuation assessment | `completed` |
| `propose_followup` | action proposal | `awaiting_authority` |

`await_action_authorization`, `await_execution`, and `blocked` are passive controller states.  They
produce typed local receipts and cannot call any step adapter.

Each adapter manifest freezes its code, configuration, step, and complete authority bindings.  The
aggregate set additionally binds the exact controller identity, controller-manifest hash,
worker-manifest hash, and process principal.  It rejects partial sets, runtime rebinding, duplicate
adapter identities, changed shared authorities, or overlap among execution, validator, admission,
database, and Kernel principals/keys.  Signed authorities must be separately deployed and their
manifests must declare that private keys are absent from the worker.  The later deployment campaign
must independently prove that declaration; this Python contract is not process-memory attestation.

Receipts remain operational evidence.  An active non-proposal step must return a content-addressed
artifact or a typed non-retryable blocker.  Proposal adapters must wait for a separately signed
Kernel command.  Only the admission adapter may claim independent observation admission, and it
must simultaneously report the exact Kernel incorporation commit.

## Verification

The focused suite covers all eight active routes, all three passive states, exact recovery
projection forwarding, stale-plan rejection, manifest mutation after composition, exhaustive-set
enforcement, actual controller/worker deployment binding, authority/key overlap, typed blockers,
and receipt semantics.  The existing controller, restart, runtime, redrive, vertical-cut, and PR-6
compatibility tests continue to pass.

This is control-path engineering evidence.  It does not demonstrate a production validator,
qualified execution image, live process restart, target-host deployment, or scientific result.

## Remaining gates

The repository now has production-boundary adapters for atomic signed execution registration,
independent validation, and atomic admission/Kernel incorporation. The observation adapters
reconstruct raw runs from SEA plus verified PR-4 terminal material and reload committed validation
from canonical PostgreSQL bytes. Their signer/database/Kernel services remain externally injected,
so this source slice does not commission or colocate those authorities.

The repository now has source-slice implementations for proposal materialization, protocol
compilation persistence, and continuation custody. PR-7d composes them into the complete checked-in
worker through operation-closed, receipt-authenticated Unix RPC clients. It still needs the actual
deployment-owned service processes, artifact/receipt byte custody, ACL/policy commissioning, and
the external Kernel signer deployment. A
graph-scoped F9-v2 campaign service and write-once archive now exist as a source/test slice, but its
production assessor, process isolation, and key custody are not commissioned. The terminal
dispatcher now consumes a mechanically verified PR-4 terminal lineage through PR-7c's
public-key-only factory; its target-host read-only ACL, filesystem/key custody, and live restart
campaign remain open. The PR-6 evaluation leaf now has its fixed-path handler, candidate image, and
atomic SEA/PR-4 registration source slice; the built image and exact host remain unqualified. The
checked-in factory does not commission those endpoints or prove that signing private keys are
absent from worker memory on a target host.

The PR-4 Linux/root/systemd/loop/ext4/rootful-Docker campaign, multi-process PostgreSQL kill/restart
campaign, supervision/monitoring, and independent signer/validator deployment remain release gates.

See [ADR 0053](architecture/0053-controller-step-authority-boundary.md), the
[PR-7a runtime guide](PR7_CONTROLLER_PRODUCTION_RUNTIME.md), the
[PR-7c terminal guide](PR7C_VERIFIED_TERMINAL_DISPATCHER.md), and the
[PR-7d worker guide](PR7D_COMPLETE_CONTROLLER_WORKER.md), and the
[end-to-end architecture](END_TO_END_AUTONOMOUS_RESEARCH_ARCHITECTURE_2026_08_22.md). See also
[ADR 0056](architecture/0056-independent-observation-controller-steps.md).
