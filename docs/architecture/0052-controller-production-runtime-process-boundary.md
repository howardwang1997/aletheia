# ADR 0052: Controller production runtime process boundary

- Status: accepted
- Date: 2026-08-25

## Context

PR-5 made controller tasks, outbox delivery, lease recovery, and delivery redrive durable, but the
repository did not contain a production process that invoked dispatcher or reconciler loops. A
generic dynamic worker would close that liveness gap by reopening the legacy driver and by giving
one process every authority. Loading the full `ResearchKernelStore` merely to publish an outbox row
would also expose trust-root, policy, and archive custody to an operational dispatcher.

## Decision

Use one independently supervised process per role. A runtime deployment content-binds the
controller manifest, one composition config, and the exact source bytes of one reviewed factory.
The loader executes those already-hashed bytes and rejects legacy-driver object origins. The
factory returns a closed dependency container; cross-role or unused privileged dependencies fail.

The standard PostgreSQL factory initially owns only `kernel_dispatcher` and
`delivery_reconciler`. Kernel delivery uses an authority-minimal outbox port with no command, CAS,
policy, or audit surface. Terminal dispatch and worker execution remain unavailable until their
separate authority adapters are explicitly composed.

Runtime receipts are hash-bound operational monitoring data with no scientific authority. Process
errors fail fast; durable database transactions and task leases provide recovery.

## Consequences

- Dispatcher/reconciler invocation no longer depends on an embedding test harness.
- A compromised operational outbox process does not receive Kernel signing or replay custody.
- Process identity and executable composition cannot drift without changing externally pinned
  bytes.
- Production still needs separate terminal and step factories, key custody, supervision manifests,
  and a real kill/restart campaign; this ADR does not call the host qualified.
