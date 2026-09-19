#!/usr/bin/env python3
"""Commission the ARL-2 dry-run authorities and commit the quest activation.

One pass, run as the driver uid on the box (the CAS root this script creates
is the one the driver later opens writable, so both must share one uid):

    sudo -u arl2drv env PYTHONDONTWRITEBYTECODE=1 \
        /opt/aletheia/python/bin/python scripts/author-arl2-authorities.py \
        --working-root /opt/aletheia/arl2-dryrun \
        --cas-root /opt/aletheia/research-kernel-cas-arl2dry-20260917 \
        --database-url 'postgresql+psycopg://arl2drv@/<db>?host=/run/postgresql' \
        --spec /opt/aletheia/arl2-dryrun/configs/activation-spec.json

What it does, in order (mirrors tests/research_kernel/test_store.py
_quest_fixture/_problem_command and tests/research_controller/
test_arl2_runtime.py:967-1048):

1. generates the quest-activation authority (trust root + certified policy,
   four role keys plus the admission signer's second ORDINARY key) and the
   auditor and qualifier signing keys, all 0400 under the working root;
2. creates the writer CAS root once (0700, this uid; never recreated);
3. archives charter v1, commits it, reads the real tail event sha from the
   store audit;
4. archives problem v1, signs it against the charter tail, commits, reads
   the problem tail;
5. archives question v1 (grounding = exactly three objects, shas taken from
   the spec's file paths), signs it against the problem tail, commits, reads
   the question tail.

committed_at is stamped by the PostgreSQL transaction clock inside every
event sha, which is why signing is interleaved with committing rather than
offline.  Outputs under the working root: keys/, spool/activation/*.json
(the three canonical command bytes later pinned by the driver config), and
configs/arl2-activation-{authority,state}.json for the downstream kit
scripts.  A failed quest is abandoned whole (abort doctrine): re-run with a
fresh spec label after moving the old state file aside.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_POLICY_DOCUMENTS = (
    "safety",
    "ethics",
    "license",
    "privacy",
    "egress",
    "budget",
    "approval",
    "publication",
)

_ROLE_FIELDS = ("commissioning", "ordinary", "amendment", "emergency")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--working-root",
        required=True,
        help="absolute path to the window working root (keys/, configs/, spool/)",
    )
    parser.add_argument(
        "--cas-root",
        required=True,
        help="absolute path to the writer CAS root; created once by this script",
    )
    parser.add_argument(
        "--database-url",
        required=True,
        help="campaign database URL (peer-auth unix socket form)",
    )
    parser.add_argument(
        "--spec",
        required=True,
        help="path to the activation spec JSON (charter/problem/question content)",
    )
    parser.add_argument(
        "--valid-hours",
        type=int,
        default=96,
        help="key and charter validity in hours from now (default 96)",
    )
    return parser


def _fail(message: str) -> None:
    raise SystemExit(f"author-arl2-authorities: {message}")


def _canonical_bytes(payload) -> bytes:
    from aletheia.research_kernel.schemas import canonical_json_bytes

    return canonical_json_bytes(payload)


def _write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        _fail(f"refusing to overwrite existing key file {path}")
    path.write_bytes(payload)
    path.chmod(0o400)


def _write_canonical(path: Path, payload) -> tuple[str, str]:
    data = _canonical_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(data)
        staged = Path(handle.name)
    os.replace(staged, path)
    return str(path), hashlib.sha256(data).hexdigest()


def _identity(prefix: str, label: str, seed: str) -> str:
    digest = hashlib.sha256(f"{label}:{seed}".encode()).hexdigest()
    return f"{prefix}_{digest[:32]}"


def derived_program_id(quest_id: str) -> str:
    """Program scope frozen into the quest stream at activation.

    The campaign-request authoring reads this value from the activation
    state instead of re-deriving it, so the stream's frozen scope binding
    and the controller launch request cannot disagree (verify_launch_audit
    refuses a program mismatch).
    """
    return "prg_" + hashlib.sha256(f"{quest_id}:arl2-dryrun-program".encode()).hexdigest()[:32]


def _load_spec(path: Path) -> dict:
    import json

    spec = json.loads(path.read_text())
    for section in ("problem", "question"):
        if not isinstance(spec.get(section), dict):
            _fail(f"spec is missing the {section!r} object")
    documents = spec.get("policy_documents")
    if not isinstance(documents, dict) or set(documents) != set(_POLICY_DOCUMENTS):
        _fail(f"spec policy_documents must carry exactly {list(_POLICY_DOCUMENTS)}")
    grounding = spec["question"].get("grounding_object_paths")
    if not isinstance(grounding, list) or len(grounding) != 3:
        _fail("spec question.grounding_object_paths must list exactly three files")
    return spec


def main() -> int:
    args = _parser().parse_args()
    os.environ["ALETHEIA_DATABASE_URL"] = args.database_url

    from aletheia.research_kernel.commands import (
        ResearchCommandProposal,
        ResearchScopeBinding,
        authorize_research_proposal,
    )
    from aletheia.research_kernel.policy import (
        ResearchAuthorizationKey,
        ResearchAuthorizationPolicyProposalV1,
        ResearchAuthorizationRole,
        ResearchAuthorizationTrustKey,
        ResearchAuthorizationTrustRootV1,
        certify_research_authorization_policy,
        ed25519_key_id,
        ed25519_public_key_hex,
    )
    from aletheia.research_kernel.schemas import (
        CharterActivatedPayload,
        EventType,
        EvidenceKind,
        EvidenceRef,
        ProblemAdmittedPayload,
        QuestionAdmittedPayload,
        QuestionKind,
        ResearchCharterVersion,
        ResearchProblemVersion,
        ResearchQuestionVersion,
    )
    from aletheia.research_store.cas import FilesystemResearchArchive
    from aletheia.research_store.store import ResearchKernelStore
    from aletheia.schema_migrations import require_schema_exact

    working_root = Path(args.working_root).resolve(strict=False)
    cas_root = Path(args.cas_root).resolve(strict=False)
    if not working_root.is_dir():
        _fail(f"working root {working_root} does not exist")
    state_path = working_root / "configs" / "arl2-activation-state.json"
    if state_path.exists():
        _fail(
            "activation state already exists; a failed quest is abandoned whole "
            "(abort doctrine) - move the old state aside and use a fresh spec label"
        )
    require_schema_exact()

    spec_path = Path(args.spec).resolve(strict=True)
    spec = _load_spec(spec_path)
    label = spec.get("quest_label") or "arl2-dryrun"
    seed = uuid.uuid4().hex
    now = datetime.now(timezone.utc).replace(microsecond=0)
    valid_from = now - timedelta(minutes=5)
    expires_at = now + timedelta(hours=args.valid_hours)

    principals = spec.get("principals") or {}
    principal_for = {
        role: principals.get(role, "human:principal-investigator")
        for role in _ROLE_FIELDS
    }

    # 1. keys: one activation authority (root + four role keys) + auditor/qualifier.
    private_keys: dict[ResearchAuthorizationRole, bytes] = {
        role: os.urandom(32) for role in ResearchAuthorizationRole
    }
    key_inventory = {}
    keys_root = working_root / "keys" / "activation"
    for role in ResearchAuthorizationRole:
        path = keys_root / f"{role.value}.key"
        _write_private(path, private_keys[role])
        public = ed25519_public_key_hex(private_keys[role])
        key_inventory[role.value] = {
            "path": str(path),
            "key_id": ed25519_key_id(public),
            "public_key_ed25519_hex": public,
            "principal_id": principal_for[role.value],
        }
    # The admission signer holds a SECOND ORDINARY key whose principal is the
    # ACTION_PROPOSAL binding principal itself: the scientific bridge requires
    # the ACTION_PROPOSED event's signing principal to equal the proposer and
    # the ACTION_AUTHORIZED signing principal to differ from it, so the two
    # events can never share one policy key (contradiction #10, remedy a).
    admission_private = os.urandom(32)
    admission_path = keys_root / "admission-ordinary.key"
    _write_private(admission_path, admission_private)
    admission_public = ed25519_public_key_hex(admission_private)
    key_inventory["admission_ordinary"] = {
        "path": str(admission_path),
        "key_id": ed25519_key_id(admission_public),
        "public_key_ed25519_hex": admission_public,
        "principal_id": principals.get("action_proposal", "service.arl2.action-proposal"),
    }
    auditor_key = os.urandom(32)
    qualifier_key = os.urandom(32)
    auxiliary = {}
    for name, raw in (("auditor", auditor_key), ("qualifier", qualifier_key)):
        path = working_root / "keys" / name / f"{name}.key"
        _write_private(path, raw)
        public = ed25519_public_key_hex(raw)
        auxiliary[name] = {
            "path": str(path),
            "key_id": ed25519_key_id(public),
            "public_key_ed25519_hex": public,
        }

    root_private = private_keys[ResearchAuthorizationRole.COMMISSIONING]
    root_public = ed25519_public_key_hex(root_private)
    trust_root = ResearchAuthorizationTrustRootV1(
        trust_root_id=_identity("rat", f"{label}:trust", seed),
        frozen_at=valid_from - timedelta(days=1),
        commissioning_keys=(
            ResearchAuthorizationTrustKey(
                key_id=ed25519_key_id(root_public),
                principal_id=principal_for["commissioning"],
                public_key_ed25519_hex=root_public,
                valid_from=valid_from,
                expires_at=expires_at,
            ),
        ),
    )
    def _inventory_role(name: str) -> ResearchAuthorizationRole:
        # the admission signer's inventory entry names the second ORDINARY
        # key, not a fifth role; every other inventory name is a role verbatim
        if name == "admission_ordinary":
            return ResearchAuthorizationRole.ORDINARY
        return ResearchAuthorizationRole(name)

    role_keys = tuple(
        sorted(
            (
                ResearchAuthorizationKey(
                    key_id=item["key_id"],
                    principal_id=item["principal_id"],
                    role=_inventory_role(name),
                    public_key_ed25519_hex=item["public_key_ed25519_hex"],
                    valid_from=valid_from,
                    expires_at=expires_at,
                )
                for name, item in key_inventory.items()
            ),
            key=lambda item: item.key_id,
        )
    )
    quest_id = _identity("qst", f"{label}:quest", seed)
    program_id = derived_program_id(quest_id)
    policy_proposal = ResearchAuthorizationPolicyProposalV1(
        policy_id=_identity("rap", f"{label}:policy", seed),
        quest_id=quest_id,
        trust_root_sha256=trust_root.trust_root_sha256,
        frozen_at=valid_from - timedelta(hours=2),
        keys=role_keys,
    )
    policy = certify_research_authorization_policy(
        policy_proposal,
        trust_root=trust_root,
        root_key_id=trust_root.commissioning_keys[0].key_id,
        private_key=root_private,
        certified_at=now,
    )

    # 2. writer CAS root: created exactly once, owned by this uid.
    if cas_root.exists():
        metadata = os.lstat(cas_root)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            _fail(f"CAS root {cas_root} exists with variant custody")
    else:
        cas_root.mkdir(mode=0o700)
    cas_metadata = os.stat(cas_root)

    authority_path, _ = _write_canonical(
        working_root / "configs" / "arl2-activation-authority.json",
        {
            "schema_name": "aletheia.arl2_activation_authority",
            "schema_version": 1,
            "quest_id": quest_id,
            "trust_root": trust_root.model_dump(mode="json"),
            "policy": policy.model_dump(mode="json"),
            "role_keys": key_inventory,
            "auxiliary_keys": auxiliary,
            "valid_from": valid_from.isoformat(),
            "expires_at": expires_at.isoformat(),
            "database_url_sha256": hashlib.sha256(args.database_url.encode()).hexdigest(),
            "cas_root": str(cas_root),
            "cas_device_id": cas_metadata.st_dev,
            "cas_inode": cas_metadata.st_ino,
            "cas_owner_uid": cas_metadata.st_uid,
            "cas_group_gid": cas_metadata.st_gid,
            "cas_directory_mode": oct(stat.S_IMODE(cas_metadata.st_mode)),
            "generated_at": now.isoformat(),
        },
    )

    archive = FilesystemResearchArchive(cas_root)
    store = ResearchKernelStore(trust_root=trust_root, archive=archive, genesis_policy=policy)

    def _role(name: str):
        # the policy now holds two ORDINARY keys (the admission signer's is
        # the second); select by the inventory's key_id so the activation
        # commands always sign under the role's own minted key
        wanted = key_inventory[name]["key_id"]
        return next(item for item in policy.keys if item.key_id == wanted)

    def _commit_and_tail(command, event_type: EventType):
        store.commit(command)
        events = store.audit(quest_id).events
        if events[-1].event_type is not event_type:
            _fail(
                f"expected {event_type.value} at the stream tail after commit, "
                f"found {events[-1].event_type.value}"
            )
        return events[-1]

    # 3. charter v1.
    documents = {}
    for name in _POLICY_DOCUMENTS:
        document = Path(spec["policy_documents"][name]).resolve(strict=True)
        documents[name] = hashlib.sha256(document.read_bytes()).hexdigest()
    root_branch_id = _identity("rbr", f"{label}:root", seed)
    charter = ResearchCharterVersion(
        quest_id=quest_id,
        charter_id=f"charter:{uuid.uuid4().hex}",
        version=1,
        mission=spec["mission"],
        value_boundaries=tuple(spec.get("value_boundaries") or ("scientific_integrity",)),
        included_scopes=tuple(spec.get("included_scopes") or ("bounded_research",)),
        allowed_action_classes=tuple(spec.get("allowed_action_classes") or ("analysis",)),
        **{f"{name}_policy_sha256": digest for name, digest in documents.items()},
        amendment_principal_ids=(principal_for["amendment"],),
        emergency_stop_principal_ids=(principal_for["emergency"],),
        authorized_by_principal_id=principal_for["commissioning"],
        authority_receipt_sha256=hashlib.sha256(spec_path.read_bytes()).hexdigest(),
        authorized_at=now,
        expires_at=expires_at,
    )
    archive.archive_object(charter)
    charter_proposal = ResearchCommandProposal(
        quest_id=quest_id,
        scope_binding=ResearchScopeBinding(quest_id=quest_id, program_id=program_id),
        expected_stream_version=0,
        expected_tail_event_sha256=None,
        event_type=EventType.CHARTER_ACTIVATED,
        payload=CharterActivatedPayload(
            charter_ref=charter.object_ref,
            root_branch_id=root_branch_id,
        ),
        proposed_by_principal_id=principal_for["commissioning"],
        proposed_at=now,
    )
    charter_command = authorize_research_proposal(
        charter_proposal,
        idempotency_key=f"arl2dry:{quest_id}:charter-activation",
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=_role("commissioning").key_id,
        private_key=private_keys[ResearchAuthorizationRole.COMMISSIONING],
        authorized_at=now,
        source_event_key=f"arl2dry:{quest_id}:charter-activation",
    )
    charter_tail = _commit_and_tail(charter_command, EventType.CHARTER_ACTIVATED)
    # Every later command is timestamped from the previous event's
    # DB committed_at: the stream's own clock, so each commit verifies
    # authorized_at <= committed_at by construction instead of racing a
    # wall-clock offset against the local socket's commit latency.
    problem_at = charter_tail.committed_at

    # 4. problem v1, signed against the real charter tail.
    problem_spec = spec["problem"]
    problem = ResearchProblemVersion(
        problem_id=f"problem:{uuid.uuid4().hex}",
        quest_id=quest_id,
        charter_ref=charter.object_ref,
        version=1,
        title=problem_spec["title"],
        statement=problem_spec["statement"],
        scope=problem_spec["scope"],
        importance_rationale=problem_spec["importance_rationale"],
        unknowns=tuple(problem_spec.get("unknowns") or ()),
        semantic_delta=problem_spec.get("semantic_delta") or "initial problem version",
        authored_by_principal_id=principal_for["commissioning"],
        authored_at=problem_at,
    )
    archive.archive_object(problem)
    problem_proposal = ResearchCommandProposal(
        quest_id=quest_id,
        scope_binding=ResearchScopeBinding(quest_id=quest_id, program_id=program_id),
        expected_stream_version=1,
        expected_tail_event_sha256=charter_tail.event_sha256,
        event_type=EventType.PROBLEM_ADMITTED,
        payload=ProblemAdmittedPayload(
            problem_ref=problem.object_ref,
            branch_id=root_branch_id,
        ),
        proposed_by_principal_id=principal_for["commissioning"],
        proposed_at=problem_at,
    )
    problem_command = authorize_research_proposal(
        problem_proposal,
        idempotency_key=f"arl2dry:{quest_id}:problem-admission",
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=_role("ordinary").key_id,
        private_key=private_keys[ResearchAuthorizationRole.ORDINARY],
        authorized_at=problem_at,
        source_event_key=f"arl2dry:{quest_id}:problem-admission",
    )
    problem_tail = _commit_and_tail(problem_command, EventType.PROBLEM_ADMITTED)
    question_at = problem_tail.committed_at

    # 5. question v1: exactly three grounding refs, signed against the
    #    problem tail.
    question_spec = spec["question"]
    grounding_paths = [Path(item).resolve(strict=True) for item in question_spec["grounding_object_paths"]]
    grounding_shas = sorted(
        hashlib.sha256(path.read_bytes()).hexdigest() for path in grounding_paths
    )
    question = ResearchQuestionVersion(
        question_id=f"question:{uuid.uuid4().hex}",
        quest_id=quest_id,
        charter_ref=charter.object_ref,
        problem_ref=problem.object_ref,
        version=1,
        kind=QuestionKind(question_spec.get("kind", "comparative").lower()),
        statement=question_spec["statement"],
        scope=question_spec["scope"],
        answer_space=tuple(question_spec["answer_space"]),
        scientific_value=question_spec["scientific_value"],
        falsifiability=question_spec["falsifiability"],
        evidence_refs=tuple(
            EvidenceRef(
                kind=EvidenceKind.POLICY,
                object_sha256=sha,
                object_id=f"grounding:{sha[:32]}",
            )
            for sha in grounding_shas
        ),
        semantic_delta=question_spec.get("semantic_delta")
        or "initial question version for the bounded campaign",
        authored_by_principal_id=principal_for["commissioning"],
        authored_at=question_at,
    )
    archive.archive_object(question)
    question_proposal = ResearchCommandProposal(
        quest_id=quest_id,
        scope_binding=ResearchScopeBinding(quest_id=quest_id, program_id=program_id),
        expected_stream_version=2,
        expected_tail_event_sha256=problem_tail.event_sha256,
        event_type=EventType.QUESTION_ADMITTED,
        payload=QuestionAdmittedPayload(
            question_ref=question.object_ref,
            branch_id=root_branch_id,
        ),
        proposed_by_principal_id=principal_for["commissioning"],
        proposed_at=question_at,
    )
    question_command = authorize_research_proposal(
        question_proposal,
        idempotency_key=f"arl2dry:{quest_id}:question-admission",
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=_role("ordinary").key_id,
        private_key=private_keys[ResearchAuthorizationRole.ORDINARY],
        authorized_at=question_at,
        source_event_key=f"arl2dry:{quest_id}:question-admission",
    )
    question_tail = _commit_and_tail(question_command, EventType.QUESTION_ADMITTED)

    spool = working_root / "spool" / "activation"
    command_files = {}
    for name, command in (
        ("charter", charter_command),
        ("problem", problem_command),
        ("question", question_command),
    ):
        command_files[name] = _write_canonical(spool / f"{name}.json", command)

    _write_canonical(
        state_path,
        {
            "schema_name": "aletheia.arl2_activation_state",
            "schema_version": 1,
            "quest_id": quest_id,
            "program_id": program_id,
            "root_branch_id": root_branch_id,
            "authority_manifest_path": authority_path,
            "trust_root_sha256": trust_root.trust_root_sha256,
            "policy_sha256": policy.policy_sha256,
            "charter_object_sha256": charter.object_sha256,
            "problem_object_sha256": problem.object_sha256,
            "question_object_sha256": question.object_sha256,
            "charter_event_sha256": charter_tail.event_sha256,
            "problem_event_sha256": problem_tail.event_sha256,
            "question_event_sha256": question_tail.event_sha256,
            "stream_version_after_activation": 3,
            "grounding_object_sha256s": grounding_shas,
            "grounding_object_paths": [str(path) for path in grounding_paths],
            "command_files": command_files,
            "question_version": question.model_dump(mode="json"),
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        },
    )
    sys.stdout.write(
        f"quest {quest_id} activated\n"
        f"  program {program_id}\n"
        f"  charter tail  {charter_tail.event_sha256}\n"
        f"  problem tail  {problem_tail.event_sha256}\n"
        f"  question tail {question_tail.event_sha256}\n"
        f"  state {state_path}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
