# PR-8h independent observer and qualification target campaign

- Status: observer/campaign source complete; no target-host campaign receipt exists
- Date: 2026-09-01

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

Generation w, frozen from merge commit `1a9fde2dc0e7ad6aec104390a912513f73b9a78b`
after green pull-request and main CI, proved that lease repair on the real target. Its deterministic
source archive had SHA-256
`cf44ac59fdbd43266bb4a00d1f0f071957e91cb0400771dc777ed5f23d3a7dea`; its
release-freeze receipt had SHA-256
`c529866bcba8ac93f3c262eb64762b83bede913fa3300f72aac4fca77058cb98`.
Execution `exe_8d07017297ea17b5cd8b5e64b98c0dc8`, attempt
`iat_16a768fb98dab7464a20596ab0eeda12` and scientific slot
`sos_e59816303024d353b7f77717c0e620fd` launched successfully. The database atomically moved the
attempt from the 3,600-second pre-launch lease into the ordinary heartbeat window at its first
runtime launch authorization, then retained a healthy running attempt under repeated short lease
extensions.

The generation-w campaign request and plan had SHA-256
`cdbf81312cb9928bc972e37b0e9929ab7eab0f92b230205d1951eb6fa0124369` and
`64cbed6de41a27fe2e3984cfe55740848ba524ea85e3b5d27b221f2a49fc417e`.
Activation completed and the real loop-backed ext4 runtime started, but the independently bounded
installed-manifest observation then exposed an undersized source contract: the deployment spec
allowed at most 60 seconds even though exhaustive rehashing covers the root-owned code and Python
trees, loaded native objects, process state, Docker state and the complete PostgreSQL catalog.
Both the initial apply and one exact journal resume failed closed at the same phase with identical
stderr SHA-256
`b054fb378a963ddba189afa9bf7a8b466ae5486bcf33b68b20cb9a87e3375df3`.
Neither run wrote `02-installed-manifest.json`, and neither entered the campaign SIGKILL or
PostgreSQL-backend-termination phases. These are retained negative engineering results, not a
campaign receipt.

The observation remains bounded, but its closed upper limit is now five minutes rather than one.
A target request must explicitly select that budget and an observation TTL at least as large;
duration still cannot exceed TTL, TTL remains capped at five minutes, and the independent campaign
deadline remains the outer bound. Synthetic regression tests accept exactly 300 seconds and reject
both 301 seconds and any duration that exceeds its signed TTL.

The retained generation-w runtime later exited zero and was adopted under fencing epoch 2 after
its heartbeat expired, but it still could not be promoted into campaign evidence. The node had a
durable sequence-2 terminal observation while PostgreSQL retained sequence 1 and no termination
challenge. A standard 30-second recovery run left the attempt projection and challenge count
unchanged. A bounded host trace then showed the worker repeatedly revalidating the same input and
terminal evidence, rewriting the same reconciliation state and retrying the rejected challenge
every 250 ms. The adapter deliberately hid the allocator's rejection text. It also discarded every
safe way to distinguish one rejection from another, turning a fail-closed decision into a silent
high-I/O hot loop.

The node boundary now carries only an irreversible SHA-256 diagnostic of the allocator exception
type and text; raw allocator text and lease credentials remain unavailable to the service log. The
service emits one canonical reconciliation status per distinct attempt/reason/diagnostic tuple,
backs identical results off exponentially to a 30-second ceiling, and does not atomically rewrite
an already-identical reconciliation state. A fresh PostgreSQL regression also proves that a real
running attempt can expire, be adopted under a rotated fence and then commit its terminal
challenge and acceptance without launching a second runtime. These changes make another target
failure bounded and mechanically distinguishable; they do not turn generation w into a passing
campaign.

Generation x, frozen from merge commit `3e13ebcdd4de193502124245869c934581b18464`
after green pull-request and main CI, proved the five-minute installed-manifest budget on the real
target and exposed an earlier Linux exec-observation race. Its deterministic source archive had
SHA-256 `928d8605b35d2ad19e3e64d59325d036e281214bdc8f03bd4782b3306a617d58`;
its release-freeze receipt had SHA-256
`f4bb157935f1111a85b2be4011f9026fe255499f94898855040702a585c1ebd0`.
Execution `exe_227940543ad2e4f87e38e492ee308382`, attempt
`iat_6b3affbb5805bec7da0f00a630707cba` and scientific slot
`sos_37c30e43bf8ee80413ff58bd877744ef` were atomically preregistered. The campaign
request and plan had SHA-256
`d59e2990eca3542939bc126802a1054b54153e4fd7df79d040a237cf498a5c0a` and
`504099b24125236f6d89df380ede5405ace125719ef512da75e766e1ffdfa2c7`.

Activation completed, and the exhaustive independently signed installed-manifest observation
finished inside the selected 300-second budget. Its 6,980,508-byte journal record had SHA-256
`3adfd4dbf18d1f19877ff21c71866ebf1b92cd7136ec8e2c76d54e9b43e63c7d`.
The node nevertheless failed before accepting runtime launch evidence. During the same-PID
launch-gate `execve` transition, one `/proc/<pid>/cmdline` read lacked canonical NUL framing and
was treated as an immediate hard error. After systemd restarted the node, the cold-recovery policy
correctly refused to manufacture pre-runtime authority for the already-started container. The
campaign therefore stopped at the running-attempt projection boundary with stderr SHA-256
`3be5cd5cfca7d784698dbe7d8e3250bdead91149de68873ee9159fbdb9e55034`.
No campaign service SIGKILL, campaign loop/ext4 mount or PostgreSQL backend termination ran.

The exact retained container later exposed a stable 175-byte, NUL-terminated fd-shebang argv for
the pinned workload; those bytes have SHA-256
`b53ccf9d08721bda706ce4c81f5b84cb851e86b4f3cb83b0e48c15b51945dc88`,
and the corresponding node journal has SHA-256
`cba74b77046df03b48aba5540b208a745e16625e5ddfa1510905d7336f0ab036`.
The initial-launch observer now treats non-canonical cmdline framing only as an uncommitted,
bounded transition. Every retry revalidates the exact container ID, PID, start identity and full
engine configuration; no transient bytes are accepted, and a persistent malformed projection
still fails when the bounded window expires. All five generation-x units were stopped and
disabled, the exact container removed, its mounts unmounted and `/dev/loop23` detached; database,
journal, backing-image and framing evidence remain retained.

Generation y, frozen from merge commit `0cf3c8ea43f5ce13e7fd6335d480b6b123c266de`
after green pull-request and main CI, proved that bounded exec observation on the real target and
then exposed a PostgreSQL-only parser defect in the campaign runner. Its deterministic source
archive had SHA-256 `357d375e90e904d301e74f190e5c418616b614e51414aa20804dcdbe8fa4a51b`;
its release-freeze receipt had SHA-256
`0e2f631560d8fb8841332d0d5656168f0eefe891fb57d776c65d4790ac9ff449`.
Execution `exe_045560c2976ba8381f266bb25b1afcce`, attempt
`iat_6986670bf6a5d50326fd6808dff4ac23` and scientific slot
`sos_7ccdde4ce202d51037ac555d6b140863` were atomically preregistered. The campaign
request and canonical plan had SHA-256
`26746ca0ca3d14114eeeca631d4b981faaf4c84778bc5d86b5e9632d71b75cc0` and
`3c2f9b7a331825c2561488f1b33f6a356ad30c4ec8e34f7495b623682f08e090`.

Activation completed with journal SHA-256
`535971b28196fdf3f77ed44d3d4890c1a15145a7d15f08d175b82d54a3b217cd`, and the
6,980,508-byte installed-manifest record completed inside the five-minute bound with SHA-256
`34350d02c245ae4ec4530cb6acc5a2fdeafad4dc90b403fca03d5b4e02bddb35`.
The node remained on its original MainPID with zero systemd restarts, retained local phase
`running`, and the exact 1,800-second container remained live. This is the target behavior that
generation x could not establish; no transient cmdline bytes were admitted.

The campaign then failed before its first deliberate process kill because its attempt-projection
SQL used PostgreSQL's reserved `AUTHORIZATION` keyword as an unquoted relation alias. PostgreSQL
17 rejected `authorization.authorization_sha256` at parse time, while the adapter reduced that
`SQLAlchemyError` to the expected fail-closed `campaign attempt projection is unavailable` text.
On the retained rows, the otherwise identical query with the non-reserved alias `sea` returned
exactly the running attempt, authorization and scientific slot. The repair freezes that statement
in one source constant, uses `sea` throughout, and adds both a source-level alias regression and a
real migrated PostgreSQL CI parse/execute test. No node/outbox/quota/watchdog SIGKILL, campaign
loop/ext4 probe or PostgreSQL backend termination ran. All five generation-y units were stopped
and disabled, the exact container was removed, its three mount projections were unmounted and
`/dev/loop23` was detached. The database, 16 MiB backing image, node journal and root-only SQL
diagnostic archive remain retained as negative engineering evidence.

Generation `20260901a`, frozen from merge commit
`8386fb527075f13532974b08b9e6766e0a1f2511`, proved the later persisted-SEA repair and exposed a
systemd-version compatibility defect in the first destructive node kill. Execution
`exe_fe40510d18d615211bac47369cdfa458`, attempt
`iat_704688c3f5a9a0d4c7977710edab1e0a` and slot
`sos_698891ea720a97b89dc6fa2b3c1bf0ad` were registered for campaign request SHA-256
`ecc59c2c3a489415cb5fe4751617f9962e004e1693d46c9a7619a3bea111943b`. Activation, the
6,980,508-byte installed-manifest observation and outbox quiescence were durably recorded, but
systemd 249 rejected the newer `--kill-whom=main` spelling before sending a signal. The 111-byte
stderr had SHA-256 `9585fbd45386144ddd3245b616d0e1665500c1d0e00b55a1c4d40dc8ec4f5340`.
No node-kill record was written. The runner now uses the systemd-249-compatible
`--kill-who=main` spelling and freezes that argv in a regression test.

Generation `20260901b`, frozen from merge commit
`7afb64ddbc8329ced9aeb92b5996c02b0bacc8b5`, then proved the real node SIGKILL/restart and retained
the full 1,800-second workload, but exposed a concrete termination-challenge construction defect.
Its execution `exe_7eaedc87b0571da7c43af1b02eafe2a5`, attempt
`iat_ad8ede9e91b7e4607878bd8920a5569d` and slot
`sos_333e9059a8d533ed1f4cd0718c056207` were bound to request SHA-256
`93ac4023b9b24514a7e409f2d480c4effaf7763acf0a96009ff05e48dcf0a75e`. The journal reached
`03-node-kill.json` with SHA-256
`1ac11a087746fdbb49a7a2f40bb9a4884b90eddfe7d3df44594ab7001c6baeb0`, but the production issuer
omitted the required runtime-inspection evidence hash when constructing the closed challenge.
The allocator therefore failed closed and no terminal-campaign record was admitted. Merge commit
`e4cbe435d03a82f09d1ea3ef8eee1c291321d5ca` binds that exact evidence hash and adds a concrete
issuer regression.

Generation `20260901c`, frozen from that merge after both pull-request and main CI passed, proved
the repaired terminal path and reached the root-service recovery boundary. Its reproducible source
archive had SHA-256
`6f07ab573efa3efb3f872de49e690eb3e919f400ce5e4938c11f63516fae3cf9`; the release-freeze receipt
had SHA-256 `3b4555789da2ff74bab6b82cdd624fcd2f8676683a61c82697cb537a99fae77d`.
Execution `exe_03399f896c7faa2714b542f769eadc59`, attempt
`iat_3b3ab9aa0c73b7342299fb0afa6deec9` and slot
`sos_b87d81a4b901b845e29252ed38c27f63` were bound to request and plan SHA-256
`fb6b779647e800f9af318e4775a19900e5e175c0f3b056ad1d5725a221d5a583` and
`f59a412d828cfd0daa2f43e4442c72efc15b967c6f3c2e170988ad8557a1c61e`.

The exact rootful-Docker workload ran from `2026-09-01T02:57:53Z` through
`2026-09-01T03:27:54Z` and exited zero after the selected 1,800 seconds. The installed-manifest
record, terminal campaign, node peer and outbox peer records had SHA-256
`dbabecb6402757352ffbf8af847aa768f7737c9dd5125dbe9f98a99c782982c5`,
`a2c3b3e2b94107d2859e4c598dba1764c3dba6d88c4c4af2bfff7c052a76c939`,
`efccd7b4fe577b9ad9a8e388639c2407ccb065a58bbbbf7cd37191ed15f79c55` and
`557c00cf4f803606975db9edf5f474a497c9a55551c23a6b99799298355d150d`. Both node and outbox
SIGKILL recovery therefore completed before the runner created the real `/dev/loop27` ext4 quota
generation and killed the quota/watchdog processes.

That later phase exposed a service-readiness race, not an integrity failure. systemd reported the
restarted quota and watchdog units active at `03:28:04Z` and `03:28:06Z`, while their application
startup receipts appeared at `03:28:06.846Z` and `03:28:08.213Z`. The node-UID replay attempted
the watchdog socket at `03:28:08.120Z`, 93 milliseconds before the service became ready, and
failed closed with stderr SHA-256
`b0f63e0e9e8024958396cd8c257a5ae28be57e438efa940f487293009e6d6603`. No
`06-root-services.json`, PostgreSQL-backend-kill record, final reobservation or campaign receipt was
written. Post-kill replay now retains the child exception type and retries only the two exact
quota/watchdog `service is unavailable` results until the already-frozen campaign deadline.
Peer identity, response shape, deployment scope, durable receipt and every other error remain
immediate fail-closed outcomes. Generation-c journals, mounts, database rows and service logs are
retained as negative engineering evidence, not deployment qualification.

PostgreSQL observation no longer copies routine, trigger, sequence or owner claims out of the
deployment spec.  One `REPEATABLE READ, READ ONLY` snapshot reruns the exhaustive ACL/role gate,
hashes every live execution routine and non-internal trigger definition, reads each exact sequence
configuration and object owner, freezes the unrelated-public-routine owner baseline, and samples
the database clock.  A fresh PostgreSQL 17 target schema produced 27 routine, 70 trigger, one
sequence and 57 owner records through this path. Missing or definition-drifted expected objects,
plus unexpected execution routines and triggers on the protected tables, therefore remain visible
to preflight instead of being hidden by an expected-value echo.

The post-policy `20260902f` attempt is retained as another negative engineering result, not target
qualification. Execution `exe_5262a55a2fc652128afc5117f2ea3db0`, attempt
`iat_07e6426dda2ce12096885b033d3ebe8c` and slot
`sos_f6232f8a28edab264ff3985d5ca9af2e` reached an exact Docker start submission, but the 15-second
runtime-control ticket expired at `2026-09-02T06:34:39.360817Z` and the pinned container entrypoint
did not begin until `2026-09-02T06:34:39.449616Z`. The launch gate therefore exited with its
reserved rejection code `126` before workload exec. No runtime launch receipt or scientific raw
run exists, and the allocator correctly retained reconciliation rather than treating a generic
Docker exit as absence.

The source repair adds only a closed recovery form for that timing race. It requires the immutable
start-submission journal and historical complete container-inspection hash, entrypoint start at or
after the signed expiry, exact exit `126`, PID zero and no restart. Before container removal the
independent root watchdog revalidates the same ID/name, closed process state and timestamps plus the
complete frozen OCI enforcement projection, including a fresh hash of the runtime-owned seccomp
copy. The historical full-inspection hash remains immutable evidence, but unrelated current Docker
metadata is not required to remain byte-identical. Earlier start, any other exit code, restart,
configuration or timestamp drift, missing container evidence, or a real launch journal remains
fail-closed. The
first target replay of merge `b7d6eba` exposed a separate liveness defect: the allocator reused the
artifact-submission deadline as the selector bound for a row that provably never acquired runtime
identity, so `20260902f` was no longer delivered after `hard_deadline + artifact grace` and retained
its exclusive holds. The follow-up keeps only exact pre-runtime/no-launch-receipt cleanup
deliverable beyond that unrelated artifact window; the allocator still re-locks every authority
row and requires a fresh signed absence proof, and actual-runtime/terminal recovery remains
bounded. Merge `e0c883be6fb4ff6b3340e202432c2230a9db7a9a` was frozen and replayed on the target. That
replay proved the exact stopped exit-126 container and sealed generation-1 absence evidence, but
the source node key had already expired before the allocator transaction could accept the proof.
The container, attempt, exclusive loop/ext4 hold and immutable local/DB evidence remain retained;
there is still no absence decision or campaign receipt.

The next narrow repair does not revive that node. Alembic `20260903_0032` admits one separately
keyed, at-most-one-hour cleanup authority pinned to the exact source node manifest, existing
attempt, runtime preparation, already-committed launch authorization, next absence epoch and root
watchdog deployment. Its one-shot worker can pull only that named attempt, cannot poll generic
work, cannot launch or reauthorize, rejects any local launch receipt/runtime identity, and can
produce only a release decision with no replacement authority. A pre-expiry node-signed pending
receipt remains immutable generation 1; recovery appends a fresh generation 2 that explicitly
supersedes it. PR #139 was subsequently merged, frozen, installed and migrated on the target. Its
first two one-shot cleanup invocations made no container-removal or database-release mutation: the
first found the independently supervised services inactive, and the second exposed the watchdog's
byte-wide Docker inspection replay defect. PR #140 repaired that boundary and was merged, frozen,
installed and target-replayed under a newly commissioned, non-reused cleanup key. The third
invocation passed watchdog quiescence but exposed the equivalent raw-inspection equality in the
node cleanup replay, again before container removal or database release. A second narrow
stable-semantics follow-up must merge and target-replay under another non-reused key. See
[PR-8j attempt-scoped cleanup recovery](PR8J_ATTEMPT_SCOPED_PRE_RUNTIME_CLEANUP.md).

Generation `20260903g`, frozen from merge commit
`7ff28514e84a756d75004aa8d1a545091b12df87`, incorporated that node-side stable-replay repair and
reached a new target-only launch-timing boundary. Its campaign request and plan had SHA-256
`40c88ab645ce5de70cfa05023521ca661a1888afeca88c9f814078536f235e69` and
`f4f1ce0263b3761b911a004c0cd562b4`. Execution
`exe_54abfd9a789cc368b0602970bb8b51e4` and attempt
`iat_88dec31d6385eca0b653421404233ff7` retained campaign activation and the
`02-installed-manifest.json` record (SHA-256
`514c4a798d17aaa5b39f62622799b5d405d5552a60722e7ec2aeae64b3341f55`); the apply process never
observed the attempt as running after that checkpoint and wrote no destructive-fault phase.

The exact Docker start was submitted at `2026-09-03T02:49:04.267261Z`, and container
`49120073870a3421a052b711168277a32cfef01faa53d3e0fa57192f1bf3b95e` began at
`02:49:04.459057638Z`. The runtime ticket expired at `02:49:05.775710Z`; after rehashing the frozen
launch configuration and workload, the gate exited `126` at `02:49:05.892104134Z`, before the
node could publish `engine-launch.json`. The exact output tree is empty, but the entrypoint began
before ticket expiry, so the existing narrow expired-before-start proof correctly refuses to call
this a never-started workload. A later read-only database query showed that the durable attempt row
remains `starting` at state version 2 with an expired lease, one launch authorization, zero runtime
inspections, and no runtime identity or launch receipt. The restarted node's local journal reports
`historical_pre_runtime_cleanup_did_not_prove_absence` and a reconciliation-required outcome, but
no central reconciliation transition was committed. The row must not be relabelled to match the
local outcome. Operationally it requires reconciliation and has no scientifically admissible
output. This is negative engineering evidence, not a campaign receipt.

The allocator defect was an internally inconsistent window. The deployment selected a 30-second
maximum launch delay, but first authorization contracted the attempt/resource lease to the
15-second heartbeat interval and then truncated the signed ticket to that lease. Docker
create/start consumed almost that entire interval, leaving the in-container verifier about 1.3
seconds. First and replacement launch authorization now require the hard deadline and
runtime-control pin to cover the complete configured launch window, keep both attempt and resource
leases live for at least the greater of that window and the heartbeat extension, and issue the
ticket for the full window. After launch, ordinary heartbeats may extend but never shorten that
retained lease; pre-launch Docker and gate work can no longer be assigned a lease shorter than
their own signed authorization. PostgreSQL regressions cover the initial contraction, a launch
that remains valid after crossing the shorter heartbeat interval, and a delayed replay that must
renew a complete replacement window.

The same retained evidence exposed a separate production-composition omission. The allocator had
an atomic database-clock `reconcile_expired()` transition, but only tests called it; the polling
worker could therefore report a node-local reconciliation outcome forever while the central row
remained `starting`. Each worker tick now invokes that transition with its exact configured
`(node_id, node_manifest_sha256)` before terminal adjudication, settlement or assignment polling.
The allocator checks both deployment pins and the registered node identity, filters candidates by
node, and rechecks the locked attempt before mutation. It retains resource and budget holds and
does not release, retry or create scientific authority. A real PostgreSQL regression reproduces
the generation-g shape—committed launch authorization, no runtime identity or launch receipt, and
cleanup unable to prove absence—and requires central `starting@v2 ->
reconciliation_required@v3` plus the unchanged local diagnostic. The historical generation-g row
has not been edited; it remains evidence produced by the older frozen release.

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

## Explicit remaining gate

The selected Linux target is compatible and has run frozen/installed candidate generations, but
none has emitted the complete campaign receipt. Therefore no host is currently proven deployable,
PR-4b remains nondeployable, and `scientific_admission_allowed` is always false even in a
successful campaign receipt. The generation-`20260903g` database is already at `0032` and retains
its attempt as `starting` with an expired lease and no accepted runtime identity. Its local node
journal requires reconciliation, but the database has not persisted that transition. Because the
exact container started before ticket expiry, that attempt must not be relabelled as never-started
or passed through the attempt-scoped pre-runtime cleanup protocol.

The next ordered source operation is to merge and freeze the complete-launch-window and
node-scoped expiry-reconciliation repairs. The complete repository Conda environment must continue
to resolve non-yanked `transformers>=5.10.4,<6` and pass both `pip check` and
`pip-audit --local`. That floor does not by itself invalidate generation g's separately prepared
PR-8i service runtime: a read-only inspection of the frozen tree found 27 distributions and neither
Transformers nor sentence-transformers. Before freezing generation h, independently rehash,
re-probe and audit the exact minimal runtime tree it will pin. Reuse is admissible only if its
bytes, immutable custody, required imports, native-mapping closure and actual installed dependency
set all revalidate unchanged; otherwise prepare and rehash a fresh minimal runtime. Do not add the
unrelated research/model stack merely to make this service runtime resemble the complete repository
environment. A fresh superseding generation must use a fresh database and non-reused authority
identities, the already-proven five-minute observation budget, an explicit bounded
initial-assignment window, a short runtime heartbeat, a launch authorization whose complete
configured window is retained by the lease, and the exact 1,800-second workload. The complete
campaign must then run—not more controller authority.

A read-only target reinspection at `2026-09-03T17:23:58Z` also found all five qualification units
from each of generations `20260901c`, `20260901d` and `20260903g` still enabled and live. The
generation-c and generation-d node units had accumulated 24,628 and 21,614 restarts respectively,
while the generation-g node had restarted once. Follow-up checks found one shared
`aletheia_qualification` database at schema `20260903_0032`, one allocator plus three outbox
connections, and retained c/d/g quota devices `/dev/loop26`-`27`, `/dev/loop28`-`29`, and
`/dev/loop6`. Those sibling workers are background execution and I/O authority, not inert retained
evidence; their database and mount residue also cannot be called a fresh h environment. The
concrete campaign host now enumerates all systemd
services through the pinned `systemctl` executable and fails before campaign mutation if any live
`aletheia-qualification-*` or `aletheia-arl1-*` service outside the request's exact five units is
active, activating, reloading or deactivating. It repeats that check around activation,
installed-manifest observation and completed receipt revalidation. The check only rejects; it
never stops or disables a unit. Generation h therefore requires exact reviewed retirement of every
older active generation plus a fresh isolated database and mount review, or a fresh disposable
target, before its request is frozen.

At `2026-09-04T03:47:23Z`, an explicitly authorized operator retirement preserved the historical
request, database, journals, manifests, unit files, workspace sources and quota backing images,
while removing only c/d/g execution and mount authority. Read-only checks first bound all fifteen
units to their exact generation manifests and releases, and bound three exact Docker containers to
their exited/PID-zero identities. The operator then stopped and disabled the five c, five d and
five g units. All fifteen became `inactive/dead`, `MainPID=0` and `disabled` before further
cleanup. The exact exited containers were removed by full ID:

- c: `a50dd7ce1ed15d72b15840c28b117efb11688981d9ee0da6ece58cf65560c801`, exit 0;
- d: `0bb03fea5517bab6edc84ed282afde0185091888ba21ecf30b6ee9b59fb0e594`, exit 0; and
- g: `49120073870a3421a052b711168277a32cfef01faa53d3e0fa57192f1bf3b95e`, exit 126.

The five exact output mounts were ordinarily unmounted without lazy, recursive or forced
semantics; `/dev/loop26`, `/dev/loop27`, `/dev/loop28`, `/dev/loop29` and `/dev/loop6` were detached
only after all five had no mountpoint. The c/d/g workspace bind targets were then ordinarily
unmounted. Their five 16 MiB backing images remain root-owned, root-grouped mode `0600`; the three
runtime-journal control roots remain in allocator custody mode `0700`; and all three manifest bytes
still match the SHA-256 values embedded in their retained units. A fresh closed-system service
scan found no live sibling qualification or ARL-1 service. A forced-read-only peer query finally
confirmed that generation g is still `starting@v2`, has no reconciliation reason, runtime identity
or launch receipt, and retains both resource and budget holds. Retirement did not rewrite the
allocator.

This retirement removes the known live c/d/g contamination but does not certify the target as
fresh. Inactive historical generations, their retained containers, loops and mounts remain outside
that exact authorization. A generation-h request still requires a fresh isolated database and a
complete read-only mount/container review (or a new disposable target) after the repaired source
has merged and been frozen.

See [ADR 0081](architecture/0081-independent-qualification-target-campaign.md), the
[PR-8g commissioning guide](PR8G_QUALIFICATION_AUTHORITY_COMMISSIONING.md), and the
[PR-8b installer guide](PR8B_DISABLED_QUALIFICATION_INSTALLER.md).
