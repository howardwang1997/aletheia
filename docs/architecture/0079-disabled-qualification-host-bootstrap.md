# ADR 0079: bootstrap qualification principals and custody roots before publishing authority

- Status: Accepted
- Date: 2026-08-27

## Context

All five qualification process factories exist, but their rendered units name numeric Linux
identities and the node/outbox factories require a live database URL. The disabled file installer
cannot safely infer accounts, directory ownership or PostgreSQL connectivity. Publishing configs,
keys, ACLs and units in one operation would also make a partially commissioned host difficult to
audit and easy to activate accidentally.

## Decision

Split commissioning at an authority-free boundary. The first stage may create only exact locked
node/outbox principals and an exhaustive set of empty custody roots. It must:

1. require a canonical out-of-band SHA-pinned request and explicit disabled-only acknowledgement;
2. make each Linux user name equal its PostgreSQL peer role and give only the node user the pinned
   Docker supplementary group;
3. pin the existing Docker group, `/run/postgresql` directory and account-management executable
   bytes before mutation and revalidate them after principal creation;
4. journal every intent and completed live observation under one root-owned deployment lock;
5. accept exact replay, repair only a safe empty interrupted-directory state, and reject variants;
6. inject separate passwordless local-socket URLs only into the node and outbox unit environments,
   with all libpq override variables unset; and
7. keep config/key publication, PostgreSQL mutation, systemd installation/activation, deployment
   qualification and scientific admission mechanically false.

The fixed socket path is a deployment renderer constant rather than a new defaulted field in
`QualificationDeploymentSpecV1`; this preserves existing v1 canonical spec identities.

## Consequences

Later stages receive frozen UID/GID, directory inode and peer-auth assumptions instead of creating
them incidentally while handling secrets or SQL. A root operator must still pre-provision the
bootstrap journal parent, root-controlled target parents, Docker group and PostgreSQL socket.

This is source-level commissioning machinery, not host evidence. PR-8g subsequently adds exact
config/key publication and PostgreSQL role/ACL creation while keeping all units absent. Native
dependency closure for the account tools, disabled unit install, the independent Linux observer
and the live process-kill campaign remain later gates.
