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

The repository still needs concrete, deployment-owned implementations for proposal materialization,
protocol compilation persistence, signed execution authorization, independent F9-v2 validation,
atomic observation admission, Kernel command submission, and continuation custody. The terminal
dispatcher now consumes a mechanically verified PR-4 terminal lineage through PR-7c's
public-key-only factory; its target-host read-only ACL, filesystem/key custody, and live restart
campaign remain open. The PR-6 evaluation leaf still needs a qualified handler/image that runs
through PR-4 custody. No complete worker factory will be checked in until those exact services can
be composed without loading their signing private keys into the worker.

The PR-4 Linux/root/systemd/loop/ext4/rootful-Docker campaign, multi-process PostgreSQL kill/restart
campaign, supervision/monitoring, and independent signer/validator deployment remain release gates.

See [ADR 0053](architecture/0053-controller-step-authority-boundary.md), the
[PR-7a runtime guide](PR7_CONTROLLER_PRODUCTION_RUNTIME.md), the
[PR-7c terminal guide](PR7C_VERIFIED_TERMINAL_DISPATCHER.md), and the
[end-to-end architecture](END_TO_END_AUTONOMOUS_RESEARCH_ARCHITECTURE_2026_08_22.md).
