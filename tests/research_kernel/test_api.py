from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import aletheia.api.programs as legacy_programs_api
from aletheia.api.deps import require_access
from aletheia.api.main import app as production_app
from aletheia.api.research_kernel import get_research_kernel_store, router as kernel_router
from aletheia.config import get_settings
from aletheia.research_kernel.commands import (
    AuthorizedResearchCommand,
    ResearchCommandProposal,
    ResearchScopeBinding,
    authorize_research_proposal,
)
from aletheia.research_kernel.policy import (
    ResearchAuthorizationKey,
    ResearchAuthorizationPolicyProposalV1,
    ResearchAuthorizationPolicyV1,
    ResearchAuthorizationRole,
    ResearchAuthorizationTrustKey,
    ResearchAuthorizationTrustRootV1,
    certify_research_authorization_policy,
    ed25519_key_id,
    ed25519_public_key_hex,
)
from aletheia.research_kernel.reducer import empty_state
from aletheia.research_kernel.schemas import (
    CharterActivatedPayload,
    EventType,
    ResearchCharterVersion,
)
from aletheia.research_store.cas import FilesystemResearchArchive
from aletheia.research_store.store import (
    ResearchCommandReceipt,
    ResearchKernelStore,
    ResearchReplayAudit,
)

_AT = datetime(2026, 8, 24, 1, 2, 3, tzinfo=timezone.utc)
_QUEST_ID = "qst_" + "1" * 32
_OTHER_QUEST_ID = "qst_" + "8" * 32
_PROGRAM_ID = "prg_" + "2" * 32
_OTHER_PROGRAM_ID = "prg_" + "9" * 32
_ROOT_BRANCH_ID = "rbr_" + "3" * 32
_ROOT_PRIVATE = b"\x10" * 32
_PRIVATE_KEYS = {
    ResearchAuthorizationRole.COMMISSIONING: b"\x21" * 32,
    ResearchAuthorizationRole.ORDINARY: b"\x22" * 32,
    ResearchAuthorizationRole.AMENDMENT: b"\x23" * 32,
    ResearchAuthorizationRole.EMERGENCY: b"\x24" * 32,
}
_PRINCIPALS = {
    ResearchAuthorizationRole.COMMISSIONING: "human:commissioner",
    ResearchAuthorizationRole.ORDINARY: "agent:operator",
    ResearchAuthorizationRole.AMENDMENT: "human:amender",
    ResearchAuthorizationRole.EMERGENCY: "human:emergency",
}
_PRODUCTION_PATHS = frozenset(production_app.openapi()["paths"])


def _authority() -> tuple[ResearchAuthorizationTrustRootV1, ResearchAuthorizationPolicyV1]:
    root_public = ed25519_public_key_hex(_ROOT_PRIVATE)
    trust_root = ResearchAuthorizationTrustRootV1(
        trust_root_id="rat_" + "4" * 32,
        frozen_at=_AT - timedelta(days=3),
        commissioning_keys=(
            ResearchAuthorizationTrustKey(
                key_id=ed25519_key_id(root_public),
                principal_id="deployment:commissioner",
                public_key_ed25519_hex=root_public,
                valid_from=_AT - timedelta(days=4),
                expires_at=_AT + timedelta(days=30),
            ),
        ),
    )
    keys = []
    for role, private_key in _PRIVATE_KEYS.items():
        public = ed25519_public_key_hex(private_key)
        keys.append(
            ResearchAuthorizationKey(
                key_id=ed25519_key_id(public),
                principal_id=_PRINCIPALS[role],
                role=role,
                public_key_ed25519_hex=public,
                valid_from=_AT - timedelta(days=1),
                expires_at=_AT + timedelta(days=10),
            )
        )
    proposal = ResearchAuthorizationPolicyProposalV1(
        policy_id="rap_" + "5" * 32,
        quest_id=_QUEST_ID,
        trust_root_sha256=trust_root.trust_root_sha256,
        frozen_at=_AT - timedelta(hours=2),
        keys=tuple(sorted(keys, key=lambda item: item.key_id)),
    )
    return trust_root, certify_research_authorization_policy(
        proposal,
        trust_root=trust_root,
        root_key_id=trust_root.commissioning_keys[0].key_id,
        private_key=_ROOT_PRIVATE,
        certified_at=_AT - timedelta(hours=1),
    )


def _command_fixture() -> tuple[
    AuthorizedResearchCommand,
    ResearchCommandProposal,
    ResearchAuthorizationTrustRootV1,
    ResearchAuthorizationPolicyV1,
]:
    trust_root, policy = _authority()
    charter = ResearchCharterVersion(
        quest_id=_QUEST_ID,
        charter_id="charter:api-cutover",
        version=1,
        mission="Exercise only the signed kernel API boundary.",
        value_boundaries=("scientific_integrity",),
        included_scopes=("api_fixture",),
        allowed_action_classes=("analysis",),
        safety_policy_sha256="a" * 64,
        ethics_policy_sha256="b" * 64,
        license_policy_sha256="c" * 64,
        privacy_policy_sha256="d" * 64,
        egress_policy_sha256="e" * 64,
        budget_policy_sha256="f" * 64,
        approval_policy_sha256="0" * 64,
        publication_policy_sha256="1" * 64,
        amendment_principal_ids=(_PRINCIPALS[ResearchAuthorizationRole.AMENDMENT],),
        emergency_stop_principal_ids=(_PRINCIPALS[ResearchAuthorizationRole.EMERGENCY],),
        authorized_by_principal_id=_PRINCIPALS[ResearchAuthorizationRole.COMMISSIONING],
        authority_receipt_sha256="2" * 64,
        authorized_at=_AT,
        expires_at=_AT + timedelta(days=9),
    )
    proposal = ResearchCommandProposal(
        quest_id=_QUEST_ID,
        scope_binding=ResearchScopeBinding(quest_id=_QUEST_ID, program_id=_PROGRAM_ID),
        expected_stream_version=0,
        expected_tail_event_sha256=None,
        event_type=EventType.CHARTER_ACTIVATED,
        payload=CharterActivatedPayload(
            charter_ref=charter.object_ref,
            root_branch_id=_ROOT_BRANCH_ID,
        ),
        proposed_by_principal_id="model:planner",
        proposed_at=_AT,
    )
    commissioning = next(
        key for key in policy.keys if key.role is ResearchAuthorizationRole.COMMISSIONING
    )
    command = authorize_research_proposal(
        proposal,
        idempotency_key="api-cutover:genesis",
        authorization_policy=policy,
        trust_root=trust_root,
        authorization_key_id=commissioning.key_id,
        private_key=_PRIVATE_KEYS[ResearchAuthorizationRole.COMMISSIONING],
        authorized_at=_AT,
    )
    return command, proposal, trust_root, policy


class _FakeKernelStore:
    def __init__(self, *, scope_binding: ResearchScopeBinding) -> None:
        self.scope_binding = scope_binding
        self.committed: list[AuthorizedResearchCommand] = []

    def commit(self, command: AuthorizedResearchCommand) -> ResearchCommandReceipt:
        self.committed.append(command)
        return ResearchCommandReceipt(
            command_id=command.command_id,
            quest_id=command.quest_id,
            scope_binding=command.scope_binding,
            idempotency_key=command.idempotency_key,
            source_event_key=command.source_event_key,
            command_sha256=command.command_sha256,
            expected_stream_version=command.expected_stream_version,
            expected_tail_event_sha256=command.expected_tail_event_sha256,
            result_stream_version=1,
            result_event_sha256="3" * 64,
            result_event_id="event:api-cutover",
            result_snapshot_sha256="4" * 64,
            outbox_id="outbox:api-cutover",
            principal_id=command.principal_id,
            authorization_trust_root_sha256=command.authorization_trust_root_sha256,
            authorization_policy_sha256=command.authorization_policy_sha256,
            authorization_receipt_sha256=command.authorization_receipt_sha256,
            committed_at=_AT + timedelta(seconds=1),
            created=True,
        )

    def audit(self, quest_id: str) -> ResearchReplayAudit:
        return ResearchReplayAudit(
            quest_id=quest_id,
            scope_binding=self.scope_binding,
            events=(),
            state=empty_state(),
            verified_snapshot_sha256s=(),
        )

    def list_quests(self) -> tuple:
        return ()


@pytest.fixture
def api_fixture(monkeypatch: pytest.MonkeyPatch):
    command, proposal, trust_root, policy = _command_fixture()
    fake = _FakeKernelStore(scope_binding=command.scope_binding)
    test_app = FastAPI()
    test_app.include_router(legacy_programs_api.router)
    test_app.include_router(kernel_router)
    test_app.dependency_overrides[require_access] = lambda: {
        "id": "untrusted-http-controller",
        "role": "owner",
    }
    test_app.dependency_overrides[get_research_kernel_store] = lambda: fake
    monkeypatch.setattr(legacy_programs_api, "_STORE", fake)
    yield {
        "app": test_app,
        "command": command,
        "proposal": proposal,
        "trust_root": trust_root,
        "policy": policy,
        "store": fake,
    }
    test_app.dependency_overrides.clear()


def _command_url(program_id: str = _PROGRAM_ID) -> str:
    return f"/research-kernel/programs/{program_id}/quests/{_QUEST_ID}/commands"


def test_only_a_full_authorized_command_reaches_commit_and_http_user_cannot_rewrite_principal(
    api_fixture,
) -> None:
    command = api_fixture["command"]
    with TestClient(api_fixture["app"]) as client:
        response = client.post(_command_url(), json=command.model_dump(mode="json"))

    assert response.status_code == 200, response.text
    assert response.json()["principal_id"] == "human:commissioner"
    assert api_fixture["store"].committed == [command]
    assert api_fixture["store"].committed[0].principal_id != "untrusted-http-controller"


@pytest.mark.parametrize("body_kind", ["proposal", "legacy"])
def test_unsigned_proposal_and_legacy_request_shapes_cannot_write(
    api_fixture,
    body_kind: str,
) -> None:
    body = (
        api_fixture["proposal"].model_dump(mode="json")
        if body_kind == "proposal"
        else {
            "idempotency_key": "legacy-shape",
            "spec": {"title": "not an authorized command"},
        }
    )
    with TestClient(api_fixture["app"]) as client:
        response = client.post(_command_url(), json=body)

    assert response.status_code == 422
    assert api_fixture["store"].committed == []


def test_program_path_cannot_route_a_cross_scope_signed_command(api_fixture) -> None:
    with TestClient(api_fixture["app"]) as client:
        response = client.post(
            _command_url(_OTHER_PROGRAM_ID),
            json=api_fixture["command"].model_dump(mode="json"),
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "command belongs to another Program"
    assert api_fixture["store"].committed == []


def test_audit_and_replay_reads_enforce_the_frozen_program_scope(api_fixture) -> None:
    with TestClient(api_fixture["app"]) as client:
        audit = client.get(f"/research-kernel/programs/{_PROGRAM_ID}/quests/{_QUEST_ID}/audit")
        replay = client.get(f"/research-kernel/programs/{_PROGRAM_ID}/quests/{_QUEST_ID}/replay")
        crossed = client.get(
            f"/research-kernel/programs/{_OTHER_PROGRAM_ID}/quests/{_QUEST_ID}/audit"
        )

    assert audit.status_code == 200
    assert audit.json()["scope_binding"]["program_id"] == _PROGRAM_ID
    assert replay.status_code == 200
    assert crossed.status_code == 409


def test_old_legacy_url_is_gone_and_explicit_legacy_url_remains(api_fixture) -> None:
    assert "/research-graph/quests" not in _PRODUCTION_PATHS
    assert "/legacy/research-graph/quests" in _PRODUCTION_PATHS
    assert "/research-kernel/programs/{program_id}/quests/{quest_id}/commands" in _PRODUCTION_PATHS

    with TestClient(api_fixture["app"]) as client:
        assert client.get("/research-graph/quests").status_code == 404
        assert client.get("/legacy/research-graph/quests").status_code == 200


def _pin_file(path: Path, payload: bytes) -> str:
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _configure_custody(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    trust_root: ResearchAuthorizationTrustRootV1,
    policy: ResearchAuthorizationPolicyV1,
) -> tuple[Path, Path, Path]:
    trust_path = tmp_path / "trust-root.json"
    registry_path = tmp_path / "genesis-policies.json"
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    trust_bytes = trust_root.model_dump_json().encode("utf-8")
    registry_bytes = json.dumps(
        {
            "schema_name": "aletheia.research_genesis_policy_registry",
            "schema_version": 1,
            "policies": [policy.model_dump(mode="json")],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    settings = get_settings()
    monkeypatch.setattr(settings, "research_kernel_trust_root_path", trust_path)
    monkeypatch.setattr(
        settings,
        "research_kernel_trust_root_file_sha256",
        _pin_file(trust_path, trust_bytes),
    )
    monkeypatch.setattr(settings, "research_kernel_genesis_policy_registry_path", registry_path)
    monkeypatch.setattr(
        settings,
        "research_kernel_genesis_policy_registry_file_sha256",
        _pin_file(registry_path, registry_bytes),
    )
    monkeypatch.setattr(settings, "research_kernel_cas_root", cas_root)
    return trust_path, registry_path, cas_root


def test_production_composition_requires_exact_root_registry_and_existing_cas(
    api_fixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, _, cas_root = _configure_custody(
        monkeypatch,
        tmp_path,
        api_fixture["trust_root"],
        api_fixture["policy"],
    )

    store = get_research_kernel_store(_QUEST_ID)

    assert isinstance(store, ResearchKernelStore)
    assert store._trust_root == api_fixture["trust_root"]
    assert store._genesis_policy == api_fixture["policy"]
    assert isinstance(store._archive, FilesystemResearchArchive)
    assert store._archive.root == cas_root


def test_unconfigured_or_hash_mismatched_custody_returns_503(
    api_fixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    for field in (
        "research_kernel_trust_root_path",
        "research_kernel_trust_root_file_sha256",
        "research_kernel_genesis_policy_registry_path",
        "research_kernel_genesis_policy_registry_file_sha256",
        "research_kernel_cas_root",
    ):
        monkeypatch.setattr(settings, field, None)
    with pytest.raises(HTTPException) as missing:
        get_research_kernel_store(_QUEST_ID)
    assert missing.value.status_code == 503

    trust_path, _, _ = _configure_custody(
        monkeypatch,
        tmp_path,
        api_fixture["trust_root"],
        api_fixture["policy"],
    )
    monkeypatch.setattr(
        settings,
        "research_kernel_trust_root_path",
        tmp_path / "missing-trust-root.json",
    )
    with pytest.raises(HTTPException) as missing_file:
        get_research_kernel_store(_QUEST_ID)
    assert missing_file.value.status_code == 503

    monkeypatch.setattr(settings, "research_kernel_trust_root_path", trust_path)
    monkeypatch.setattr(settings, "research_kernel_trust_root_file_sha256", "0" * 64)
    with pytest.raises(HTTPException) as mismatched:
        get_research_kernel_store(_QUEST_ID)
    assert mismatched.value.status_code == 503


def test_symlink_and_non_regular_custody_paths_return_503(
    api_fixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trust_path, _, cas_root = _configure_custody(
        monkeypatch,
        tmp_path,
        api_fixture["trust_root"],
        api_fixture["policy"],
    )
    settings = get_settings()
    trust_link = tmp_path / "trust-root-link.json"
    trust_link.symlink_to(trust_path)
    monkeypatch.setattr(settings, "research_kernel_trust_root_path", trust_link)
    with pytest.raises(HTTPException) as symlinked:
        get_research_kernel_store(_QUEST_ID)
    assert symlinked.value.status_code == 503

    monkeypatch.setattr(settings, "research_kernel_trust_root_path", cas_root)
    with pytest.raises(HTTPException) as non_regular_control_file:
        get_research_kernel_store(_QUEST_ID)
    assert non_regular_control_file.value.status_code == 503

    monkeypatch.setattr(settings, "research_kernel_trust_root_path", trust_path)
    monkeypatch.setattr(settings, "research_kernel_cas_root", trust_path)
    with pytest.raises(HTTPException) as non_directory_cas:
        get_research_kernel_store(_QUEST_ID)
    assert non_directory_cas.value.status_code == 503


def test_registry_without_the_exact_quest_policy_returns_503(
    api_fixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_custody(
        monkeypatch,
        tmp_path,
        api_fixture["trust_root"],
        api_fixture["policy"],
    )

    with pytest.raises(HTTPException) as wrong_quest:
        get_research_kernel_store(_OTHER_QUEST_ID)

    assert wrong_quest.value.status_code == 503
