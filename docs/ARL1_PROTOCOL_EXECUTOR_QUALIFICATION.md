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

## Security fixes included with the gate

Runtime startup and `/readyz` now call `require_schema_exact()`. An Alembic head stamp without the
matching ORM-managed tables, columns, indexes, foreign keys and constraints is rejected. This
closes a real state in which a partially restored or previously stamped database reported ready
despite missing Research Kernel/controller authority structures.

The dashboard is upgraded from vulnerable Next.js 15.1.6 to 16.3.3. Production `npm audit` is
clean at this checkpoint. The removed `next lint` command is replaced with ESLint's supported flat
configuration, and the previously hidden TypeScript/React findings are fixed rather than ignored.

## What is and is not complete

The qualification contract, compiler replay, target-campaign replay, independent-verifier seam,
signature path, deterministic report and tamper tests are implemented. The target campaign must
complete before any protocol execution, validator and execution-authorization timestamps must
precede execution, source verification cannot predate the evidence it attests, and offline receipt
verification independently reapplies policy validity bounds. No production ARL-1 qualification
receipt has been issued.

This development machine is macOS and cannot supply the required Linux/root/systemd/cgroup-v2/
rootful-Docker/AppArmor/loop-ext4 target evidence. The existing PR-8h source tests use synthetic
host ports and are intentionally ineligible because every ARL evidence contract requires
`synthetic_evidence=false`. A production source-verifier composition must also replay the retained
database/CAS evidence instead of returning a recording test receipt.

Therefore the honest current status remains **ARL-1 qualification gate implemented, deployed
system not ARL-1 qualified**.

## Test commands

Run the focused qualification and schema gates:

```bash
conda run -n aletheia pytest -q \
  tests/execution/test_arl1_qualification.py \
  tests/test_schema_migrations.py \
  tests/execution/test_qualification_campaign.py
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
   the production controller, validator, admission and Kernel incorporation path;
5. retain the all-attempt and evidence-archive manifests plus deterministic reports;
6. run a production `ARL1EvidenceVerificationPort` under a principal separate from the ARL signer;
7. issue the signed receipt, restart from empty process memory, and freshly verify the complete
   bundle again; and
8. tamper one byte in every retained source class and require verification to fail.

No local test or CI badge substitutes for those target-host steps.
