# Private prospective suite custody runbook

This runbook operates F7's private evaluation boundary. It is for the evaluator owner and
independent auditor, not the Aletheia research loop. Completing the commands below proves that the
custody protocol works; it does not prove that a real private suite exists or that Aletheia passes
the Frontier Gate.

The governing decision is
[`ADR 0007`](../adr/0007-private-suite-custody-and-retirement.md). The four-arm plan and statistics
contract is in [`BASELINE_MATRIX.md`](BASELINE_MATRIX.md).

## 1. Roles and storage

Use distinct identities and access boundaries for:

| Role | May access | Must not access |
|---|---|---|
| Research principal | validation analogs, public prompts, submission inbox | test/task manifests, hidden assets, gold, KMS, custody ledger |
| Custody owner | custody index, suite/task ciphertext policy, first approval | research runtime, sole approval authority |
| Independent auditor | custody index, reviews, ledger heads, second approval | research runtime, sole approval authority |
| Evaluator operator | authorized ciphertext/KMS calls, materialized evaluator files | changing frozen matrix or acceptance config |
| Domain reviewer | assigned source and gold evidence | candidate submissions and development tuning |

The custody ledger and evaluator root should live under an evaluator-only OS/service account.
Archive each emitted ledger head in an independently controlled audit system. Hash chaining without
an external head anchor cannot detect a complete rewrite by the storage owner.

## 2. Commission prospective tasks

Before authoring test content:

1. freeze a contamination cutoff;
2. commission tasks after that cutoff outside Aletheia's development context;
3. create a validation analog with the same structural family but different content and manifest
   hash;
4. record provenance, license/retention terms, human-subjects status where applicable, and a
   similarity/overlap audit;
5. obtain an independent domain review binding gold evidence and acceptable conclusions;
6. set a retirement time after review.

A formal suite requires 10–20 tasks, at least two domains, and coverage of true effect, null
effect, confounding, label error, distribution shift, and insufficient sample. A `critical`
contamination risk or known material training overlap is rejected at schema validation.

## 3. Seal each role separately

The object store must expose four role prefixes:

```text
custody://suite-manifests/...
custody://task-manifests/...
custody://hidden-assets/...
custody://gold-evidence/...
```

For every envelope record ciphertext and plaintext byte counts and SHA-256 identities, the
encryption scheme, a SHA-256 identity of the external key ID, and a SHA-256 identity of the access
policy. Do not serialize a key name, token, wrapped data key, credential, nonce-bearing plaintext
bundle, private prompt, or answer into the custody manifest.

Task manifest, hidden asset, and gold evidence must use three distinct key identities. Task and
hidden assets also require different access-policy identities. KMS authorization should give the
research account no decrypt permission and should permit the evaluator role only during the
approved window.

## 4. Freeze the test bindings

Create a `PrivateSuiteManifest` only after these immutable identities exist:

- encrypted `EvaluationSuite` plaintext hash;
- evaluator manifest hash;
- test-phase `BaselineMatrixPlan` hash and its validation-parent hash;
- acceptance configuration hash;
- custody owner, independent auditor, and research principal identities;
- all task custody records.

Validate without reading ciphertext or spending access:

```bash
conda run -n aletheia python scripts/manage_private_suite.py validate \
  --manifest evaluator/private_suite_manifest.v1.json
```

Register the exact version in the append-only custody ledger:

```bash
conda run -n aletheia python scripts/manage_private_suite.py register \
  --manifest evaluator/private_suite_manifest.v1.json \
  --ledger evaluator/custody/events.v1.jsonl
```

Registration is idempotent for identical content. Reusing the same suite ID/version with different
content fails.

## 5. Issue bounded two-person authorization

The authorization must bind the private manifest, encrypted evaluation suite, evaluator, matrix,
acceptance config, and all four derived run-plan hashes. Its principals must exactly match the
frozen custody owner and auditor. Record two different hashes of externally authenticated approval
artifacts. A hash is an audit binding, not signature verification; verify the actual approval
artifacts before invoking this command.

The window must start after suite freeze and fit the manifest TTL (72 hours by default, seven days
maximum):

```bash
conda run -n aletheia python scripts/manage_private_suite.py authorize \
  --manifest evaluator/private_suite_manifest.v1.json \
  --authorization evaluator/private_access_authorization.v1.json \
  --ledger evaluator/custody/events.v1.jsonl
```

Do not expose a timestamp override. The CLI intentionally evaluates the real evaluator clock when
opening access.

## 6. Provide the external store/KMS operator

The operator is deployment code outside the research repository. The CLI imports an explicit
`MODULE:CALLABLE`; the callable receives frozen models plus a non-secret configuration and returns a
ciphertext store and decryptor:

```python
def build(*, manifest, authorization, matrix, config):
    store = ObjectStoreClient(config["object_store_profile"])
    decryptor = KMSDecryptor(config["kms_profile"])
    return {"store": store, "decryptor": decryptor}
```

`store.read_ciphertext(storage_ref) -> bytes` resolves only opaque custody references.
`decryptor.decrypt(envelope, ciphertext) -> bytes` obtains credentials from the evaluator workload
identity or secret manager. Never put key bytes in command arguments, the operator config, the
manifest, or the custody ledger.

Exercise the store, KMS role, free disk space, evaluator permissions, and scoring runtime with a
separate pilot suite. A post-open failure retires the real suite by design.

## 7. Consume the one-time unlock

Choose a new receipt path. An existing receipt path fails before the operator plugin is loaded:

```bash
PYTHONPATH=/opt/aletheia-evaluator-operators \
conda run -n aletheia python scripts/manage_private_suite.py materialize \
  --manifest evaluator/private_suite_manifest.v1.json \
  --authorization evaluator/private_access_authorization.v1.json \
  --matrix evaluator/baseline_matrix.test.v1.json \
  --ledger evaluator/custody/events.v1.jsonl \
  --operator-factory private_kms_operator:build \
  --operator-config evaluator/private_operator_refs.json \
  --evaluator-root evaluator/runtime \
  --output evaluator/private_materialization_receipt.v1.json
```

The command verifies all ciphertext before atomically claiming the one-time access. It then
decrypts and verifies every bound plaintext, checks the exact suite/matrix/run plans, materializes
only suite/task manifests and hidden scorer assets, and leaves gold evidence in memory. Files are
new-only and mode `0400`. The receipt contains identities, not plaintext paths or values.

If status already contains `materialization_receipt_sha256` but the external receipt file was lost
in a crash, do not call `materialize` again. Verify the staged plaintext and recover the committed
receipt without using KMS:

```bash
conda run -n aletheia python scripts/manage_private_suite.py recover-materialized \
  --manifest evaluator/private_suite_manifest.v1.json \
  --ledger evaluator/custody/events.v1.jsonl \
  --access-id frontier-private-access-2026q3 \
  --evaluator-root evaluator/runtime \
  --output evaluator/recovered_materialization_receipt.v1.json
```

Failure interpretation:

| Failure point | Access spent? | Required response |
|---|---:|---|
| ciphertext missing/hash mismatch | no | repair evaluator storage, then retry within authorization |
| authorization expired/wrong binding | no | issue a conforming authorization; never alter matrix |
| decrypt/plaintext/manifest mismatch | yes | suite is retired; investigate KMS/source, commission new version |
| disk/path collision after open | yes | partial plaintext is removed; commission new version |
| process killed after `access_opened` | yes | run `fail-materialization`; never resume or reopen |
| process killed after `suite_materialized` | already committed | run `recover-materialized`; do not decrypt again |
| custody ledger integrity failure | no new attempt | stop and compare independently archived ledger head |

If status shows `opened_access_id` but neither a materialization receipt nor failure event, preserve
the crash/incident artifact, hash it externally, and execute the terminal recovery:

```bash
conda run -n aletheia python scripts/manage_private_suite.py fail-materialization \
  --manifest evaluator/private_suite_manifest.v1.json \
  --ledger evaluator/custody/events.v1.jsonl \
  --access-id frontier-private-access-2026q3 \
  --evaluator-root evaluator/runtime \
  --error-evidence-sha256 <sha256-of-crash-evidence>
```

The command records failure, inventories and removes any exact suite/task/hidden plaintext left by
the killed process, writes a cleanup receipt, closes access, and retires the version. It is safe to
retry after an interruption. Unexpected or modified content stops automated deletion for operator
review. Do not call `materialize` again for this suite.

## 8. Construct guarded runners and execute once

Every `IndependentEvaluationRunner` for this matrix must receive a
`PrivateSuiteAccessGuard(manifest=..., ledger=..., authorization_id=...,
evaluator_principal_sha256=..., evaluator_root=...)`. Any `retire_after_access` task without a guard
is rejected before an attempt is claimed.

Pass those four guarded evaluator-owned runners to the baseline operator factory described in
[`BASELINE_MATRIX.md`](BASELINE_MATRIX.md). Do not copy materialized task manifests or hidden files
into a candidate container. The runner exposes only the public task view and submission inbox.

If the process restarts while the matrix is incomplete, use the baseline matrix's ledger-based
resume. Do not unlock or materialize the private suite again. A residual nonterminal attempt still
requires evaluator adjudication; scientific failures never authorize a retry.

## 9. Close and dispose plaintext

After every preregistered cell reaches an auditable terminal state, remove verified plaintext and
close the one-time access:

```bash
conda run -n aletheia python scripts/manage_private_suite.py close \
  --manifest evaluator/private_suite_manifest.v1.json \
  --ledger evaluator/custody/events.v1.jsonl \
  --access-id frontier-private-access-2026q3 \
  --evaluator-root evaluator/runtime
```

Cleanup verifies the exact suite/task files and all files below the suite's private hidden scope.
Modified, unexpected, special, or symlinked content stops cleanup for operator review. A successful
cleanup receipt is stored in the ledger, access is closed, and a one-time suite becomes retired.
The command is idempotent after a successful close.

Encrypted audit ciphertext remains for the manifest's retention period. Physical secure erasure is
the storage platform's responsibility.

## 10. Contamination and explicit retirement

The candidate can declare contamination in its submission; the runner records it, skips hidden
scoring, disposes plaintext, and closes the suite. A scorer can do the same through a contamination
canary. Any later attempt is denied before claim.

For an operator or development disclosure, prepare `PrivateContaminationReport` and run:

```bash
conda run -n aletheia python scripts/manage_private_suite.py report-contamination \
  --manifest evaluator/private_suite_manifest.v1.json \
  --report evaluator/contamination_report.v1.json \
  --ledger evaluator/custody/events.v1.jsonl \
  --evaluator-root evaluator/runtime
```

`--evaluator-root` is mandatory when plaintext is active. A development leak may be reported before
authorization without it; the version becomes permanently ineligible to open.

For scheduled, post-use, or operator retirement, prepare a `PrivateRetirementRecord` containing the
two frozen principals and two distinct approval-evidence hashes:

```bash
conda run -n aletheia python scripts/manage_private_suite.py retire \
  --manifest evaluator/private_suite_manifest.v1.json \
  --retirement evaluator/private_retirement.v1.json \
  --ledger evaluator/custody/events.v1.jsonl \
  --evaluator-root evaluator/runtime
```

## 11. Audit status

```bash
conda run -n aletheia python scripts/manage_private_suite.py status \
  --manifest evaluator/private_suite_manifest.v1.json \
  --ledger evaluator/custody/events.v1.jsonl \
  --output evaluator/private_custody_status.v1.json
```

Before accepting a run, the independent auditor should confirm:

- the archived pre-run head matches the registered/authorized chain;
- exactly one `access_opened` exists;
- materialization succeeded once and no failure event exists;
- run plans and acceptance configuration match the authorization;
- all contamination reports are disclosed;
- access has a cleanup receipt and is closed;
- evaluator attempt ledger and signed scorer receipts reconcile through the baseline aggregate;
- no statement calls a pilot, synthetic contract test, or public diagnostic a private scientific
  result.

Issue 11 now supplies the validation-calibrated acceptance configuration, raw receipt
reaggregation, custody-linked verdict, and immutable report described in
[`FRONTIER_GATE_REPORT.md`](FRONTIER_GATE_REPORT.md). Operators must still commission the real
suite, authenticate approvals, freeze production systems, run the public and private matrices, and
close this custody lifecycle. Until those external operations happen, the project has the complete
engineering mechanism but no Frontier Gate pass.
