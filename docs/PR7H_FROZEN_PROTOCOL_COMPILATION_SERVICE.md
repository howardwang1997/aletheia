# PR-7h frozen protocol-compilation service

- Status: source composition complete; target-host commissioning pending
- Date: 2026-08-26

## What is now runnable

The PR-7e server can load
`aletheia.research_controller_protocol_compilation_runtime.build_protocol_compilation_rpc_service`
for the single `compile_protocol` endpoint. The service locks and replays the authorized Kernel/CAS
action, retrieves one complete deployment-frozen `ProtocolCompilationRequest`, applies the existing
author/category/catalog/compiler policy, runs the pure canonical compiler, re-audits the Kernel,
and appends the accepted or blocked result to the existing compilation registry.

The baseline provider is an exact-action catalog. Every entry binds one action object hash and kind
to one canonical request hash and complete request bytes. The request already contains graph scope,
protocol version/revision, author/time, objective, world model, controls, analysis, capabilities,
resource budget, and catalogs. An unlisted action returns
`protocol_compilation:no_frozen_action_template`; there is no generative or catch-all fallback.

The compilation service now requires a preparation verifier. It checks a new provider result before
compilation and reconstructs every durable winner against the currently deployed exact template on
restart. A stored request is therefore not trusted merely because it still passes the generic IR
and compiler contracts.

The duplicate-free canonical config pins controller/worker/service identity, the powerless
compilation authority, compiler and template-provider policies, complete template catalog, provider
source bytes, database URL hash/schema revision, and read-only Kernel CAS. The process loads no
Kernel/observation/execution signing key, execution port, mutable template source, or model callback.

## Local verification

Focused tests cover accepted and blocked canonical compilation, exact restart recovery without
provider reinvocation, template/action/request/principal rebinding, missing-template blockers,
provider implementation and policy drift, duplicate/rebound config, exact RPC operation partition,
and the guarded PR-7e loader.

## Remaining release gates

This is a safe preauthored-protocol baseline, not autonomous knowledge-grounded protocol design. A
new authorized action must have a complete reviewed template in the next byte-pinned deployment
config or compilation stops. A future authoring service may replace the catalog only behind the
same closed context, verifier, canonical compiler, and durable-registry boundary.

No target host is commissioned. The exact Linux account must still prove socket, PostgreSQL, CAS,
transport-key, supervisor, alert, and process-restart custody. Eight other PR-7e service factories,
including all execution, validation, admission, and Kernel-signing authorities, remain incomplete.

See [ADR 0065](architecture/0065-frozen-protocol-compilation-rpc-service.md),
[ADR 0059](architecture/0059-durable-protocol-compilation-step.md), the
[PR-7e server guide](PR7E_EXTERNAL_RPC_SERVICE_RUNTIME.md), and the
[PR-7d worker guide](PR7D_COMPLETE_CONTROLLER_WORKER.md).
