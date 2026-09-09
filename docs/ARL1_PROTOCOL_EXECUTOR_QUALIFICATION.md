# ARL-1 Protocol Executor system qualification

## What this slice adds

`aletheia.arl1` is the frozen system-level qualification boundary for
`ARL-1 Protocol Executor`. It can issue an Ed25519-signed receipt only when one complete evidence
bundle passes all of these checks together:

- every cumulative ARL-0 gate is present in canonical order: ledger replay, sandbox isolation,
  hidden-boundary enforcement, all-attempt retention, claim ceiling, exact database structure and
  dependency audit;
- the exact PR-8h Linux target request and receipt pass their native offline verifier;
- every given protocol recompiles byte-for-byte to its retained accepted work order;
- the selected scientific-executor node preregisters at least two exact reexecutions and retained
  evidence covers every preregistered execution exactly;
- engineering completion, a validator frozen before execution, independent validation, admission,
  Kernel incorporation, all-attempt retention, reproduction and a deterministic report are all
  bound by content hash; and
- a policy-approved independent source-verification port freshly rehashes source bytes and replays
  their authorities before the separate qualification signer is allowed to sign.

The final receipt is scope-specific and expires. Its immutable claim ceiling is
`bounded_protocol_execution_engineering`. It explicitly sets autonomous research design,
scientific validity, independent replication and scientific authority to false.

## Deployment requirements

Runtime startup and `/readyz` require the exact current Alembic revision and matching live schema.
The resolved Conda environment must pass `pip check` and dependency audit. Deployment manifests
pin the source, Python runtime, identities, keys, service operations and custody roots.

The live Research Kernel CAS and F9-v2 campaign archive now support one closed shared-custody
layout for UID-separated services: only the designated writer owns a `0750` tree, every published
object is sealed `0440`, and readers must have a different non-root UID with the exact shared
primary GID. Startup derives the process's effective owner/group/other permissions and refuses a
reader that is writable or cannot traverse the root. Parent directories and objects are freshly
checked for stable inode ownership, exact modes, symlinks and group/world write bits on every read.
Publication writes and fsyncs private staging bytes before a per-object lock and atomic rename make
the single-link object visible. The artifact verifier separately accepts `0440` only as immutable
group-readable data; it does not grant artifact mutation. These rules enable cross-service replay
without making the validator, database bridge, admission service, controller or qualification
auditor a peer writer.

## What is and is not complete

The qualification contract, compiler replay, target-campaign replay, independent verifier,
signature path, deterministic report and tamper tests are implemented. The concrete verifier
freshly reopens the write-once evidence archive, PostgreSQL receipts, artifact CAS, F9-v2 campaign
archive and Research Kernel CAS/ledger; it also runs exact executable/input-pinned ARL-0 gate
commands without a shell. A single authorized action can preregister an exhaustive two-to-100-slot
campaign in one database transaction before the first qualification reservation. The target
campaign must complete before any protocol execution, validator and execution-authorization
timestamps must precede execution, source verification cannot predate the evidence it attests,
and offline receipt verification independently reapplies policy validity bounds.

Production one-shot entrypoints now exist for the given-protocol campaign and for three separate
qualification phases: source replay/signing, qualification signing, and keyless fresh audit. Each
phase requires Linux, an exact process identity, an out-of-band deployment-manifest byte digest,
canonical no-duplicate-key JSON, exact code/database/schema/inode pins and an explicit operation
acknowledgement. The source verifier and qualification signer load only their own 0400 Ed25519 key;
the auditor loads neither private key and must use a third application principal. CLI output is the
exact canonical JSON accepted by the next phase, without an added newline.

The verifier runtime also requires a byte-pinned capability-source configuration. Its separate
trust and runtime inventories bind the auditor and qualifier public keys, implementation sources,
environment identities and every selected capability. Each audit retains its signed decision and
scoped check results with their source inputs. Authority, applicability, failure, retry, calibration,
safety, license, egress and retention rules must retain their referenced bodies, as must every schema.
Signed audit materials and checked inputs cover these contract sources as well as the runtime sources.
Preparation, issuance and keyless audit freshly reopen these materials. The source verifier checks
authenticity, custody and contract bindings; the delegated capability auditor remains responsible
for the meaning and adequacy of the checks. These engineering records confer no scientific authority.

Qualification and later audit timestamps are not accepted from evidence JSON. The issuance and
verification manifests contain at most a 24-hour approved operation window; after fresh source
replay the runtime reads PostgreSQL `clock_timestamp()` through the pinned database, derives the
receipt expiry from a bounded validity duration, and checks the clock again before releasing the
result. This prevents a stale manifest from backdating, future-dating or reviving an expired
qualification receipt.

After atomically preregistering and reserving every replicate, the campaign invocation may receive
one signed `raw_run:terminal_material_pending` source status while a node is still running. It waits
only for that closed status, uses the service-signed bounded retry interval, and stops no later than
the authorization's observation-admission deadline. Missing registration, invalid signatures,
database drift, rebound terminal material and custody failures are never retried as readiness.

`--register-only` emits the complete registration receipt and exits before requesting terminal
material. It uses the same deployment pins and acknowledgement as execution. This allows an
operator to reserve the complete campaign, quiesce readers, run the node under its private custody
pins, and publish terminal artifacts before restarting independent verification. The subsequent
full invocation replays that registration and consumes the same scientific slots. The complete
handoff procedure is in the [fresh-generation runbook](GENERATION_I_REQUALIFICATION_AND_ARL1_EXIT_RUNBOOK_2026_09_06.md).

Committed validation and primary admission are recovered by scientific slot before any new
challenge is requested. Admission recovery re-verifies the original signed validation/admission,
row projection and Kernel receipt. A missing slot is distinct from corrupt or rebound authority.
The campaign atomic-admission service permits `commit_and_incorporate` and
`load_committed_admission`; the controller worker retains its original operation partition.

**No production ARL-1 qualification receipt has been issued.** The next complete deployment must
use a fresh release and commissioning window. Local tests are development verification and cannot
satisfy target-host qualification.

## Test commands

Run the focused qualification and schema gates:

```bash
conda run -n aletheia pytest -q \
  tests/execution/test_arl1_qualification.py \
  tests/execution/test_arl1_verifier.py \
  tests/execution/test_arl1_campaign.py \
  tests/execution/test_arl1_runtime.py \
  tests/execution/test_arl1_qualification_runtime.py \
  tests/observations/test_execution_registration.py \
  tests/observations/test_scientific_bridge.py \
  tests/observations/test_sources.py \
  tests/research_controller/test_execution_registration_rpc_runtime.py \
  tests/research_controller/test_external_rpc.py \
  tests/research_controller/test_external_rpc_server.py \
  tests/research_controller/test_raw_run_source_rpc_runtime.py \
  tests/research_controller/test_worker_composition.py \
  tests/test_schema_migrations.py \
  tests/execution/test_qualification_campaign.py
```

Run the shared-custody and UID-separation regression gates:

```bash
conda run -n aletheia pytest -q \
  tests/research_store/test_cas.py \
  tests/execution/test_artifact_store.py \
  tests/observations/test_f9_v2_validation.py \
  tests/research_controller/test_worker_composition.py \
  tests/research_controller/test_f9_v2_validation_rpc_runtime.py \
  tests/research_controller/test_atomic_admission_rpc_runtime.py
```

Audit the resolved Python environment and the separately frozen legacy-evaluation image
resolution:

```bash
conda run -n aletheia pip check
conda run -n aletheia pip-audit --local
conda run -n aletheia pip-audit \
  -r configs/capabilities/legacy-evaluation-runtime-constraints-v1.txt
```

Run frontend validation:

```bash
cd frontend
npm ci
npm run lint
npm run typecheck
npm run build
npm audit --omit=dev
```

The real exit procedure must use a disposable qualified Linux target:

1. migrate a fresh PostgreSQL database and require both `alembic check` and
   `require_schema_exact()` to pass;
2. execute PR-8f bootstrap, PR-8g commissioning and PR-8b installation;
3. run PR-8h and retain its request, journal, signed observations and final receipt;
4. execute each policy-required given protocol with all preregistered exact reexecutions through
   the production controller, bounded terminal-material wait, validator, admission and Kernel
   incorporation path;
5. retain the all-attempt and evidence-archive manifests plus deterministic reports;
6. run each byte-pinned given-protocol campaign with
   `scripts/run-arl1-protocol-campaign.py --apply --acknowledge
   RUN_ARL1_PROTOCOL_CAMPAIGN`, retaining its canonical stdout and SHA-256;
7. run `scripts/run-arl1-qualification.py prepare` under the source-verifier principal with exact
   acknowledgement `PREPARE_ARL1_EVIDENCE_BUNDLE`, then run `issue` under the disjoint
   qualification principal with `ISSUE_ARL1_QUALIFICATION`;
8. restart from empty process memory and run `scripts/run-arl1-qualification.py verify` under a
   third, keyless auditor principal with `VERIFY_ARL1_QUALIFICATION`; and
9. tamper one byte in every retained source class and require verification to fail.

The entire sequence must fit the freshly commissioned authority windows. Retain the campaign
receipt and each disjoint qualification phase. No local test or CI badge substitutes for these
Linux target-host steps.
