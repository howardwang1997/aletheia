# PR-8h independent observer and qualification target campaign

Status updated 2026-09-09. Deployment qualification is scoped to the exact signed target
receipt and frozen installation. It does not confer ARL-1 system qualification or scientific
validity. A fresh deployment containing current source requires its own commissioning and
qualification; historical receipts cannot qualify new bytes or renew an expired authority.

## What this slice closes

PR-8h supplies the missing concrete read-only Linux observer and the explicit destructive campaign
runner. It does not turn local unit tests into deployment evidence. A host becomes
`deployment_qualified=true` only when the checked-in runner completes on the exact commissioned
target and emits one canonical `QualificationTargetCampaignReceiptV1`.

The independent observer freshly proves:

- Linux root, real PID 1 systemd, cgroup v2, synchronized time and a UUID boot identity;
- the unchanged PR-8f/PR-8g/PR-8b receipt chain, every installed file inode/hash/mode and every
  service-owned root;
- both exhaustive root-owned code/Python trees, including streamed verification of native objects
  larger than the control-file limit; every tree file must be a single-link regular file and every
  directory/file owner, mode, byte length and digest must match the reviewed manifest;
- exact loaded unit fragments with no drop-ins or pending daemon reload, plus each live process's
  `/proc` UID/GID/groups, argv, Python inode, cwd, required/unset environment and effective,
  permitted, bounding and ambient capability bitmaps;
- a root:root Docker daemon, rootful systemd cgroups, pinned Docker-root inode/parent chain and an
  exact seccomp/AppArmor security projection;
- shared mount identity across PID 1, quota, node and Docker namespaces;
- fresh OCI layout/launch-gate evidence, exhaustive reviewed code/Python/native dependency trees,
  and the loaded enforcing AppArmor profile;
- exact PostgreSQL cluster/schema/ACL/role/object-owner state, all unrelated public-routine owners,
  and bounded host/database clock agreement; and
- a new Ed25519 observation signed by a root-only key that is distinct from node, runtime,
  validator, admission and Kernel authorities.

The deployment spec's existing Docker and authority-bundle digests remain opaque external review
pins. The observer separately freezes and compares a typed live Docker projection; it does not
replace a pre-installation review pin with a post-installation hash and create a hash cycle.
Likewise, privileged service configs freeze only their unit names; the observer is the independent
post-installation authority for exact unit bytes and inode custody, avoiding a future-unit
config/manifest self-reference while retaining the same fail-closed live check.
The observer also supplies the first durable evidence of the kernel-assigned shared-workspace
mount ID. Pre-install configs cannot truthfully predict it; live node composition pins it for the
process lifetime and same-boot revalidation rejects any later mount-identity drift.

## Destructive campaign and evidence order

The request embeds the complete observer config and one already-committed
`AtomicScientificExecutionRegistrationReceipt`. It is qualification-only, fixes a 60–7200 second
deadline, and requires the literal opt-in acknowledgement. The runner acquires one root-owned
journal lock and executes/resumes these ordered checkpoints:

1. enable/start the exact five units and prove the four long-running MainPIDs;
2. freeze an independently signed installed-manifest instance;
3. wait for the exact pre-registered attempt to be `running`, stop the outbox, SIGKILL the node
   MainPID and observe systemd replace it;
4. require exactly one successful immutable v2 terminal row while the outbox is stopped, start the
   outbox, observe its canonical `0400` spool file, SIGKILL the outbox MainPID, then prove the same
   spool inode and bytes survive replay;
5. connect as the real node and outbox Linux UIDs through passwordless local PostgreSQL peer rules;
6. create one real loop device/ext4 quota mount, call both root-service health protocols, SIGKILL
   quota and watchdog MainPIDs, and require exact durable receipt replay after restart;
7. have a node-UID peer transaction hold an advisory transaction lock, terminate its backend from
   the separately pinned admin connection, then prove connection loss, automatic lock release and
   a new peer connection;
8. retain a final fresh independent signed observation beside its mechanically recomputed preflight,
   then revalidate the terminal DB/spool authority.

The terminal phase is deliberately early: the selected workload must remain running long enough
for the installed-manifest observation, after which the runner stops the outbox and kills the node
immediately. A target fixture must therefore be a bounded, pre-registered, long-running CPU-only
workload whose normal successful output is preserved when the polling node process—not its Docker
container—is killed.

The checked-in qualification smoke workload now accepts an exact
`--minimum-runtime-seconds 0..3600` hold before it atomically publishes the unchanged deterministic
output. The target campaign must explicitly select a nonzero bounded value in its signed workload
argv; the default remains zero so the source change alone cannot be mistaken for a long-running
campaign execution. The final OCI manifest/config and launch-gate evidence must be regenerated
after this source enters `main`.

On the selected Ubuntu target, a candidate image containing these exact workload bytes was rebuilt
and exercised as UID/GID 65534 with a read-only root, no network, all capabilities dropped,
`no-new-privileges`, a private cgroup namespace, and fixed CPU/memory/PID limits. The two-second
hold completed in 3.25 wall-clock seconds including container startup and emitted only the expected
65-byte digest file. The candidate Docker 29 OCI identities are manifest
`15691f3723dd85571a84ef21ac76022835bc449721dd77e3877f9e59b8352d3e` and config
`6970da41aff96267571e000ad6fc03483196fe5939a97c56a8c502afe6e98d61`; its root-only OCI archive
is 58,859,520 bytes with SHA-256
`4cf922a48a5abbf8f3da46a3ee58c573345ccb4d471b484c234ada60f06c8e17`. The probe used the Moby
profiles commit `61eaf32614c7c71b60bd8927d3e6a4ffc8ff1f31` default seccomp bytes (SHA-256
`536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74`) and an enforcing,
qualification-named rendering of its AppArmor template (SHA-256
`83b965074431575ea2cab6c4d785aa207444a6eaa8b4415fe0c609ba84608b18`). This is a target
compatibility/build checkpoint, not a PR-8h campaign receipt.

Every completed phase is a canonical append-only journal record. Exact restart reuses recorded
evidence and revalidates a completed receipt. Root quota generations and terminal subphases have
their own internal records so a runner crash cannot silently substitute a second scientific slot.

## Operator commands

The canonical request file must be root-owned, root-group-owned, mode `0400`, and supplied with an
out-of-band SHA-256. Planning parses and hashes only; applying additionally requires Linux
effective `root:root`, the exact pinned systemd executable, `ALETHEIA_QUALIFICATION_ADMIN_DATABASE_URL`
matching the request digest, a fresh PostgreSQL head and the literal acknowledgement.

Before freezing the request, provision a separate 32-byte Ed25519 observer key as root-owned,
root-group-owned mode `0400`; bind its file digest and derived public key ID in
`QualificationLinuxObserverConfigV1`. This key is an external target-preparation input—not one of
the three execution keys commissioned by PR-8g—and must never be copied into a service config or
the canonical request bytes.

The dedicated qualification registration service must also freeze an explicit
`initial_assignment_lease_seconds` that covers registration through deterministic campaign apply
startup, while remaining inside the signed execution hard deadline. This is not the node heartbeat
interval: the node service retains its short `heartbeat_extension_seconds`, and revision `0031`
contracts the long reservation to a bounded launch lease covering the greater of that heartbeat
extension and the complete signed launch-authorization window.

```console
sudo /opt/aletheia/python/bin/python -S -s -P \
  /opt/aletheia/source/scripts/run-qualification-target-campaign.py \
  --request /root/qualification-target-campaign.json \
  --request-sha256 '<sha256>'

sudo --preserve-env=ALETHEIA_QUALIFICATION_ADMIN_DATABASE_URL \
  /opt/aletheia/python/bin/python -S -s -P \
  /opt/aletheia/source/scripts/run-qualification-target-campaign.py \
  --request /root/qualification-target-campaign.json \
  --request-sha256 '<sha256>' \
  --apply \
  --acknowledge RUN_QUALIFICATION_TARGET_CAMPAIGN
```

Do not set the admin URL on the service units; only the one-shot campaign process may receive it.
Do not reuse a normal research execution. The campaign slot and its long-running fixture must be
explicitly reserved for destructive qualification.

## How to test it

The checked-in local tests prove contract closure, canonical hashing, exact phase replay, false
scientific authority, observer signing scope, offline recomputation of the retained signed final
preflight, process capability decoding, installer-receipt reconstruction and root-service health
response binding. They use synthetic host ports and are not deployment qualification.

The real exit test must run on a disposable Linux target with rootful Docker, AppArmor, systemd,
cgroup v2, loop/ext4 tools and a fresh PostgreSQL database. Retain all of the following together:

- PR-8f bootstrap, PR-8g commissioning and PR-8b installation receipts;
- the canonical campaign request and its out-of-band digest;
- the final installed manifest, retained post-kill signed observation, campaign journal and final
  campaign receipt;
- PostgreSQL audit/count queries showing one exact attempt and one v2 terminal outbox row; and
- systemd journal excerpts spanning each recorded PID transition.

Then rerun the same apply command and require byte-identical final receipt output. Reboot/restart
tests and a fresh-database migration run are additional deployment gates; they must not edit the
receipt into a passing shape.


## ARL-1 exit

After target qualification, complete the production given-protocol campaign and all preregistered
reexecutions, then prepare the evidence bundle, issue a qualification receipt and verify it in a
fresh keyless auditor process. Every retained source class must reject byte tampering.

Current system status: a production ARL-1 qualification receipt was issued on 2026-09-09 and
passed fresh keyless verification; the tamper-rejection audit stage of the exit procedure is
still outstanding. Follow the
[current exit procedure](GENERATION_I_REQUALIFICATION_AND_ARL1_EXIT_RUNBOOK_2026_09_06.md)
and [qualification contract](ARL1_PROTOCOL_EXECUTOR_QUALIFICATION.md).
