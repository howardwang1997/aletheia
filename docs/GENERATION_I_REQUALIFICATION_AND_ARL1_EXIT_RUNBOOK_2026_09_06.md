# Fresh-generation re-qualification and ARL-1 exit runbook

Updated 2026-09-10. The historical filename is retained for links. This procedure applies to a
fresh deployment of the reviewed current source, with Alembic head `20260909_0033`.
No historical generation's receipt or expired commissioning window authorizes this deployment.

## Freeze prerequisites

- Reviewed source and migration inventories, backend checks and required PostgreSQL gates pass.
- The given-protocol campaign supports recovery from its retained validation and admission rows.
- Each selected capability binds its actual runtime and input/output contract. Retain schema and
  policy bodies, scoped engineering checks, independent signed audits and a separate qualification
  decision. Freeze the capability-source trust and runtime inventories outside the protocol;
  preparation, issuance and fresh verification require the native source verifier.
- The exact runtime tree, source archive, host identities, service operation pins, authority
  windows and resource envelope are recorded before commissioning.
- Select an unused generation identity and database after inspecting existing target state.
- Plan the complete sequence inside the fresh authority windows. Generate new deployment pins
  whenever source bytes, service operations, identities, schema or custody contracts change.
- Public documentation follows [PUBLICATION_BOUNDARY.md](PUBLICATION_BOUNDARY.md).

## 1–3: qualify the fresh deployment

1. Freeze the reviewed commit. Retain its archive SHA-256 and source/runtime manifests. Validate
   the prepared Conda runtime against this release before using it in the new installation.
2. Inspect existing target units and resources. Retire only the explicitly identified predecessor
   deployment after confirming no live workload relies on it. Preserve audit records. Create the
   fresh isolated database, migrate to `20260909_0033`, run `alembic check` and require exact schema.
3. Execute PR-8f bootstrap, PR-8g commissioning, PR-8b installation and the complete PR-8h target
   campaign. Retain canonical request, signed observations, journal and qualified target receipt;
   require exact replay of that receipt.

## 4–6: execute the protocol campaign

4. Compose the scientific services with distinct principals and freshly pinned service operations.
   The campaign database service exposes validation load, validation challenge/commit and admission
   challenge. The campaign atomic service exposes commit/incorporate and committed-admission load.
   The controller worker retains its own exact operation partition.
5. With the execution node stopped, run `run-arl1-protocol-campaign.py --register-only` using the
   same pinned deployment, `--apply` and acknowledgement as execution. Retain its complete
   registration receipt and exact replay. Stop the scientific services before handing the
   artifact store to the node under its private custody pins. Start the node, wait for every
   registered attempt's terminal acceptance, then stop it. Publish the store with the commissioned
   read-only shared modes, verify metadata and content hashes as each consuming UID/GID, and
   restart the scientific services. Preserve this sequence in a durable private operator journal;
   infer restart position from signed registration and terminal rows, never elapsed sleep alone.
6. Run the byte-pinned given-protocol campaign with all preregistered exact reexecutions. Retain
   its canonical stdout and digest, validation/admission receipts, Kernel events, all-attempt
   manifest, evidence archive and deterministic report. On restart, load and verify committed
   facts before requesting a new challenge. Resume pending execution only under a valid authority
   window and preserve existing scientific slot identities.

Do not proceed to qualification issuance without the complete campaign receipt. A local regression
or partially executed campaign cannot replace it.

## 7–9: disjoint qualification and fresh audit

7. Run `run-arl1-qualification.py prepare` as the source-verifier principal with acknowledgement
   `PREPARE_ARL1_EVIDENCE_BUNDLE`. Run `issue` as the distinct qualification signer with
   `ISSUE_ARL1_QUALIFICATION`. Each phase replays source evidence and uses pinned database time.
8. Restart from empty process memory and run `verify` as a third, keyless auditor principal with
   `VERIFY_ARL1_QUALIFICATION`. Retain the exact inputs and outputs for independent replay.
9. Schedule this stage against the retained windows before running it. Inventory every window
   the native verifier evaluates — qualification receipt validity, authority key windows, and
   the observation and admission deadlines recorded in the retained evidence — and require the
   full matrix to complete inside the earliest-closing one. Budget the measured single-case
   verifier runtime against the retained case count and the available concurrency. Run the
   native positive controls on unmodified copies before any tamper case, and stop on the first
   control failure rather than record its cases as rejections. Use isolated copies to change
   one byte in every retained source class and require rejection. Preserve original source
   material unchanged. If the budgeted matrix cannot complete inside the window, stop and
   requalify with a fresh generation rather than execute a partial matrix.

## Acceptance and authority

The final receipt must pass native offline verification and remain within its validity window.
Its claim ceiling is `bounded_protocol_execution_engineering`. PR-8h remains qualification-only;
scientific execution/admission requires its separately signed authorization. Neither receipt
establishes scientific validity, independent replication or autonomous research design.

Update the current status only from these retained artifacts. The next capability milestone is
then the bounded ARL-2 question loop in the [roadmap](LONG_TERM_ROADMAP_TO_ARL4_2026_09_06.md).
