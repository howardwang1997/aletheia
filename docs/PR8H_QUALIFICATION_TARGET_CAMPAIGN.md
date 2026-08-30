# PR-8h independent observer and qualification target campaign

- Status: observer/campaign source complete; no target-host campaign receipt exists
- Date: 2026-08-30

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

## Target compatibility checkpoint

The selected disposable Ubuntu target has passed a pre-campaign compatibility checkpoint, but not
the campaign itself.  On that host the production verifier freshly read the root-owned OCI
archive, manifest, config, ten layer diff IDs and in-image launch gate, then bound them to Docker
29.1.3's containerd-backed image inspection.  Docker 29 identifies this image by an exact OCI
manifest `Descriptor`; legacy graphdriver installations instead identify the config digest.  The
verifier accepts only those two closed representations and rejects a mixed or extended descriptor.

The same target also exposed a 31,369,824-byte root-owned Docker CLI.  Executable observation now
uses a separate 256 MiB streaming ceiling with before/after inode and parent-chain stability,
while ordinary configs, keys and control records retain their 16 MiB bounded reader.  This closes
two target-compatibility blockers without weakening either custody boundary.  It does not freeze
the final deployment manifest, install the units or create qualification evidence.

A later generation-p pre-campaign execution reached a real running container and loop-backed ext4
output mount, then exposed a Linux fd-exec detail that synthetic process fixtures had missed.  An
fd-based `execve` of a shebang script presents the live process as
`<interpreter> /dev/fd/<n> <logical arguments>` rather than preserving the script path as argv[0].
The runtime now accepts only two closed live forms: an exact direct-binary argv/executable pair, or
that exact Linux fd/shebang transformation.  The second form freshly rehashes the inherited script
descriptor against the workload pin, derives one argument-free absolute interpreter from the
pinned shebang, requires `/proc/<pid>/exe` to be the same inode and bytes as that interpreter under
the container rootfs, and repeats the complete observation after cgroup verification.  Script,
argument, interpreter, inode, ownership, mode, or mid-capture drift still fails closed.  The failed
candidate was stopped before campaign apply and retained only as diagnostic journal/backing-image
evidence; it is not a campaign receipt.

Generation q, frozen from merge commit `b95ed43fe0dafd98d3670ee8cdbbf037b67017a6`
after green main CI, then crossed that fd/shebang boundary on the same target. It retained a real
running Docker container at manifest digest
`15691f3723dd85571a84ef21ac76022835bc449721dd77e3877f9e59b8352d3e`, a `/dev/loop22` ext4
output mount, and both durable `engine-launch.json` and `launch-evidence.json` records. Its
13,856,623-byte campaign request had SHA-256
`1e045616500a6e34905b5528f6b5ecfd558d6c19704a7d369ad4e539698ee774`.

That candidate still did not produce a campaign receipt. The campaign recorded only the exact
request, plan and `01-activation.json`, then the independent installed-manifest observation
failed closed on the root watchdog process. Linux reports the effective primary GID in `Gid` and
the supplementary list separately in `Groups`; systemd's explicit empty
`SupplementaryGroups=` therefore produced an empty live list, while the observer incorrectly
invented the primary GID in that list. No campaign node/outbox/quota/watchdog SIGKILL,
PostgreSQL-backend termination, final reobservation or deployment-qualified receipt occurred.
All five q services were stopped and disabled, its exact exited container was removed, its mounts
were unmounted and `/dev/loop22` was detached; the write-once journal, database rows and backing
image remain as negative engineering evidence.

Generation r, frozen from merge commit `7973d12ffff80e9cae89c7a983fa6411b07fcb3c`
after green main CI, incorporated the supplementary-group fix and reached the next real launch
boundary. Its Docker manifest digest was
`6f3fd86d94122c9830b80fb64fda2bbf90fad5ff410d86a657200d7c9f00b977`; its
release-freeze receipt was
`2c79d79324e9722a3c1b143b2e3961d5e0021ad88010ebcd8f475dd39e956da3`.
The exact container started on `/dev/loop22`, crossed into the pinned fd/shebang workload and
remained alive, but the node captured launch evidence in the narrow interval after Docker reported
`Running=true` and before the launch gate completed `execve`. The observer therefore saw the
launch-gate argv, rejected it, and did not publish `engine-launch.json` or
`launch-evidence.json`. No campaign request/apply or destructive fault phase ran. The five services
were stopped and disabled, the container removed, all generation-r mounts unmounted and
`/dev/loop22` detached; its write-once database and journal evidence remains retained.

Initial launch capture now treats only that exact same-container, same-PID and same-`StartedAt`
argv-length transition as transient. It reruns full engine-configuration validation and fresh
process observation in a bounded window. Container/PID/start drift, configuration drift, a
wrong same-shape argv, disappearance, or timeout still fails closed; recovery never constructs
launch authority from an ambiguous already-started process.

Generation s, frozen from merge commit `ee7d56516569d71d3670c7d454798640a4912532`
after both pull-request and main CI passed, proved that transition on the real target. Its
deterministic source archive had SHA-256
`2f2fffa45808ee78af832fd955b2ee201701cf3125848f299274698c395e8745` and its
release-freeze receipt had SHA-256
`91ff529aabaa94c193db880510784ea84e47801ae8e5f7bb3e4cafac9178650e`.
Execution `exe_2b2d6f2d9a40ad294db19d0a2420458d`, attempt
`iat_7895385d67243bab36084b6055d0ea0f` and scientific slot
`sos_190a84fdb192b421492d1d5c89d03471` reached durable `running` state without a
node restart. Container `7b1e3790f63b4583886254e057e3a59ccbccf0c3ffe9bb95efc30f332c82c3e1`
published both launch records; `engine-launch.json` had SHA-256
`a0fd91bbd43440e2edf3aa7c941463642befe52b06edc46d6a0612d581a93e36` and bound
the exact pinned workload executable.

The generation-s campaign request and plan had SHA-256
`2b990769782b20a642a101d596aae6fbc2211fe6d6c6be44ee11825e122d7066` and
`61258b1e8fb741ff0a683bfe032bfba445d0c72ebc1c9ca08a471e8653e82211`.
Activation completed and was durably recorded, but installed-manifest observation then exposed a
second supplementary-group representation: for the node, systemd's explicit Docker group caused
Linux to report `Groups: 138 2101`, where `2101` was the already separately verified effective
primary GID. Empty supplementary-group root services on the same host continued to report an empty
`Groups` list. The observer failed closed before phase 02; no service SIGKILL, campaign loop/ext4
mount or PostgreSQL backend termination occurred. All five services were stopped and disabled,
the exact container removed, all generation-s mounts unmounted and `/dev/loop22` detached; the
backing image, request, activation journal and database rows remain negative engineering evidence.

Process-group verification now accepts neither representation by guesswork. It parses a unique
kernel group list, removes only the effective primary GID that is independently required in all
four `Gid` fields, and then compares the remaining closed set exactly with the frozen supplementary
GIDs. Any duplicate or any other additional/missing GID still fails closed.

Generation t, frozen from merge commit `9f746e870d5e043dad374adda26c1797ca296a05`
after green pull-request and main CI, proved the corrected process-group projection and reached a
later database transaction boundary. Its deterministic source archive had SHA-256
`52283e8abc37d8039131953ea35309b6f74287e43e651c45b643a15b34e4a5e0`; its
release-freeze receipt had SHA-256
`5db2b8a63233dc3e9316cdf06a8cc2fab98e9316f91d391f1a05d7256a84087a`.
Execution `exe_883d041449b5a658b94f548e5940ac93`, attempt
`iat_09100ab7295c81ec0fd2068be4846c29` and scientific slot
`sos_e7a33223d98bfe4e4f4d22c5ea6f1d33` launched container
`bdb1c148bd754c6bb3cb9f13b5b0ba4cc8ba11bdee72d35320b0e77777a585d7` from OCI
manifest `6f3fd86d94122c9830b80fb64fda2bbf90fad5ff410d86a657200d7c9f00b977`.
The node durably published `engine-launch.json` and `launch-evidence.json` with SHA-256
`1c13baf118c67e8860b53b913aa567ca25b4b379ab0d2d8f18b3ccd0567ee2b0` and
`61a8da71c4cfd37cbc9b5ab605b863adeedd149bc784812bd6a2cb108aef7684`. The
workload ran for the selected 1,800-second hold, exited zero and left the 65-byte
`result.sha256` with file SHA-256
`25e0da1d2cdaf722dfac6969bfde0c165e85f9a06b8db407ac18a18c7eec2547`.

That successful engine run still did not become a campaign. When the allocator accepted the
node-signed launch receipt after the short lease boundary, `accept_runtime_launch()` first dirtied
the attempt's runtime-identity columns and then queried the previous budget event. SQLAlchemy's
query-triggered autoflush sent that intermediate attempt row to PostgreSQL before the final
`state_version + 1`; the database guard correctly rejected it with `execution attempt
state_version must advance exactly once` and rolled the transaction back. The node retained the
actual launch receipt while the database retained only pre-runtime authority. On restart, the
cleanup-only delivery rejected that already-started local shape instead of resubmitting the exact
receipt, producing 98 systemd restarts before the service was stopped.

The fix keeps the budget-event lookup inside `Session.no_autoflush`, so runtime identity,
reconciliation state and the version increment reach the guard in one update. Cold recovery now
also permits only a fully reverified node-local launch receipt to cross the historical pre-runtime
delivery: it resubmits those same signed bytes without another engine start and, after lease
expiry, uses the existing fenced adoption path. Pure recovery tests and a fresh PostgreSQL target
test cover both live and expired-lease cases, including exactly one runtime start and the ordered
`reserved -> reconciliation_required -> adopted` budget events.

No generation-t campaign request, plan, apply, service SIGKILL phase, campaign loop/ext4 mount or
PostgreSQL-backend termination occurred. Its operator template also retained an `s1` release-path
suffix while all generation-t frozen files consistently bound those bytes; this was not the
transaction failure, but a later generation must use a matching release suffix. All five units
were stopped and disabled, the exact exited container was removed, both output mount projections
and the workspace bind were unmounted, and `/dev/loop22` was detached. The database rows, runtime
journal and quota backing image remain retained as negative engineering evidence.

Generation v, frozen from merge commit `f59a73aa2ebdb7f5c9d2c5c0c93cba60a118471f`
after green pull-request and main CI, reached the real campaign apply boundary. Its deterministic
source archive had SHA-256
`d682dc39976ba11a1636d25357c19a9ca4b38ead44a45698666bfea9c1041d9a`; the
release-freeze receipt had SHA-256
`83a02b3b724fb8896742fe3accc6cd6c4e3098fdeaa740342fcb4886249ba7bc`.
Execution `exe_e3bf691fcc1c7fa198ae52c23e7b6ed6`, attempt
`iat_4a8e418ce853fcf7eb3815aa77745ea2` and scientific slot
`sos_6fe31740731b0c51f553a5123bb63f73` were atomically preregistered. The campaign
request and plan had SHA-256
`41b7c593efc26c4e0006a501324bdcc512a0d1114b5a5b7952098ffe5370197c` and
`e1c5b759d8fda7e2d922db6f72d12efb052c3253d1d037492c849126fdfd6acb`.

The generation-v registration allocator used the ordinary 15-second heartbeat extension for its
initial reservation. Request generation, plan review and the apply runner's independent observer
preparation necessarily took much longer, so the DB lease had expired before systemd activated
the node. `pull_sealed_assignment()` still selected the raw envelope because it checked only the
envelope hard deadline. The node correctly rejected the expired reservation, restarted under
systemd, and disappeared while the observer read its `/proc` maps; apply failed before the
installed-manifest phase. Its exact stderr file has SHA-256
`b38d4477630fe8029ac0863550927faf2f350310e3743f570fd4033a58d5659d`.
No service SIGKILL phase, campaign loop/ext4 mount, workload container or PostgreSQL backend
termination occurred. All generation-v units were stopped and disabled and its workspace bind was
unmounted; the database rows and first journal records remain retained as negative evidence, while
the older loop and container evidence was not modified.

The repair separates the frozen `initial_assignment_lease_seconds` from the runtime heartbeat
extension. Raw sealed delivery now also requires the attempt lease and hard deadline to be live at
the database clock. At the first exact `reserved -> starting` runtime authorization, allocator
possession of the lease token atomically contracts both attempt and resource expiry to the short
heartbeat window. Alembic revision `20260831_0031` permits that contraction only when the same
transaction has written the exact append-only launch authorization carrying the new expiry; every
unbound attempt or resource rollback remains rejected by PostgreSQL.

PostgreSQL observation no longer copies routine, trigger, sequence or owner claims out of the
deployment spec.  One `REPEATABLE READ, READ ONLY` snapshot reruns the exhaustive ACL/role gate,
hashes every live execution routine and non-internal trigger definition, reads each exact sequence
configuration and object owner, freezes the unrelated-public-routine owner baseline, and samples
the database clock.  A fresh PostgreSQL 17 target schema produced 27 routine, 70 trigger, one
sequence and 57 owner records through this path. Missing or definition-drifted expected objects,
plus unexpected execution routines and triggers on the protected tables, therefore remain visible
to preflight instead of being hidden by an expected-value echo.

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
contracts to that value when it issues the first runtime launch authorization.

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

## Explicit remaining gate

The selected Linux target is compatible and has frozen/installed candidate generations, but none
has emitted the complete campaign receipt. Therefore no host is currently proven deployable,
PR-4b remains nondeployable, and `scientific_admission_allowed` is always false even in a
successful campaign receipt. The next ordered operation is to merge the pre-launch lease fix,
commission one entirely fresh generation with an explicit bounded initial-assignment window, a
short runtime heartbeat, and a 1,800-second workload inside its signed execution deadline, then
rerun the complete campaign—not more controller authority.

See [ADR 0081](architecture/0081-independent-qualification-target-campaign.md), the
[PR-8g commissioning guide](PR8G_QUALIFICATION_AUTHORITY_COMMISSIONING.md), and the
[PR-8b installer guide](PR8B_DISABLED_QUALIFICATION_INSTALLER.md).
