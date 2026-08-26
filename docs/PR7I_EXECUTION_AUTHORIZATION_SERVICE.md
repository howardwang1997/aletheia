# PR-7i scientific execution-authorization service

- Status: source composition complete; target-host commissioning pending
- Date: 2026-08-26

## What is now runnable

The PR-7e server can load
`aletheia.research_controller_execution_authorization_runtime.build_execution_authorization_rpc_service`
for the single `issue_execution_authorization` endpoint. The service accepts only an exact
`REGISTER_EXECUTION` controller tick, re-audits its current Research Kernel/CAS head, reloads the
append-only accepted compilation row, recomputes PR-4 engineering qualification from public
authority registries and fresh artifact custody, and signs the existing closed
`ScientificExecutionAuthorization` contract.

The first issuer is an exact-action catalog. Each entry freezes the already-authorized action and
adjacent Kernel events, accepted compilation and WorkOrder, qualification bundle and grant,
scientific observation artifact binding, validator/admission policies and public authority pins,
plus exact authorization and observation-deadline times. Unknown actions fail closed. There is no
dynamic qualification, protocol, policy, or time-window fallback.

The service verifies its source both before and after signing. Its frozen message makes Ed25519
signing deterministic, so an exact crash retry returns byte-identical SEA bytes and identity
without adding another mutable scientific ledger. The online verifier then rechecks the current
authorization, qualification grant, Kernel action, registries, and artifact custody at service
time.

The guarded config pins the database URL hash/schema revision, read-only Kernel CAS, complete
template catalog, issuer implementation bytes, all public verification roots, and a domain
Ed25519 private key held in a separate service-owned `0400` regular file. The domain key must be
different from the RPC receipt key. Execution authorization, qualification, pricing, budget,
terminal verification, validation, admission, artifact verification, and input resolution use
non-overlapping principals; all signing roles also use distinct keys and policies.

## Authority boundary

The pre-admission qualification verifier exposes no allocator mutation API and cannot claim that a
later qualification admission exists. This process cannot register an SEA, reserve resources,
launch work, validate observations, admit evidence, or mutate the Kernel. Those effects remain in
their separately deployed services and transactions.

## Local verification

Focused tests cover byte-identical retries, full existing SEA verification, source drift during
signing, stale controller state, missing templates, expired windows, policy/key rebinding,
read-only qualification recomputation, inability to assert a future admission, duplicate config
keys, domain/transport key separation, unsafe key mode, operation closure, and guarded factory
source pins.

## Remaining release gates

This is a safe exact-template signer, not general execution planning or host qualification. PR-7j
subsequently adds the separate `REGISTER_EXECUTION` service, which atomically preregisters this SEA
and reserves the exact PR-4 attempt without loading the SEA private key. Raw-run loading, database
attestation, independent F9-v2 validation, committed-validation loading, independent admission, and
atomic Kernel incorporation factories remain. Six PR-7e concrete service factories are therefore
still incomplete.

No target host is commissioned. Linux account/socket/PostgreSQL/CAS/registry/key ACLs,
systemd supervision, alerts, key rotation/revocation, and a fresh multi-process kill/restart
campaign remain mandatory. This source/test receipt is engineering evidence, not deployment proof
or a scientific result.

See [ADR 0066](architecture/0066-scientific-execution-authorization-rpc-service.md), the
[PR-7j registration guide](PR7J_ATOMIC_EXECUTION_REGISTRATION_SERVICE.md), the
[PR-7e server guide](PR7E_EXTERNAL_RPC_SERVICE_RUNTIME.md), the
[PR-7h compiler guide](PR7H_FROZEN_PROTOCOL_COMPILATION_SERVICE.md), and the
[PR-5 controller guide](PR5_DURABLE_SCIENTIFIC_CONTROLLER.md).
