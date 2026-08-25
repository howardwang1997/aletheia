from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from aletheia.research_kernel.policy import (
    ResearchAuthorizationError,
    ResearchAuthorizationKey,
    ResearchAuthorizationPolicyProposalV1,
    ResearchAuthorizationPolicyV1,
    ResearchAuthorizationRole,
    ResearchAuthorizationTrustKey,
    ResearchAuthorizationTrustRootV1,
    certify_research_authorization_policy,
    ed25519_key_id,
    ed25519_public_key_hex,
    verify_research_authorization_policy,
)

T0 = datetime(2026, 8, 24, tzinfo=timezone.utc)
QUEST_ID = "qst_" + "1" * 32
ROOT_PRIVATE_KEY = b"\x10" * 32
ROLE_PRIVATE_KEYS = {
    ResearchAuthorizationRole.COMMISSIONING: b"\x21" * 32,
    ResearchAuthorizationRole.ORDINARY: b"\x22" * 32,
    ResearchAuthorizationRole.AMENDMENT: b"\x23" * 32,
    ResearchAuthorizationRole.EMERGENCY: b"\x24" * 32,
}
ROLE_PRINCIPALS = {
    ResearchAuthorizationRole.COMMISSIONING: "human:commissioner",
    ResearchAuthorizationRole.ORDINARY: "agent:operator",
    ResearchAuthorizationRole.AMENDMENT: "human:amender",
    ResearchAuthorizationRole.EMERGENCY: "human:emergency",
}


def make_trust_root(*, private_key: bytes = ROOT_PRIVATE_KEY) -> ResearchAuthorizationTrustRootV1:
    public = ed25519_public_key_hex(private_key)
    return ResearchAuthorizationTrustRootV1(
        trust_root_id="rat_" + "2" * 32,
        frozen_at=T0 - timedelta(days=2),
        commissioning_keys=(
            ResearchAuthorizationTrustKey(
                key_id=ed25519_key_id(public),
                principal_id="deployment:research-commissioner",
                public_key_ed25519_hex=public,
                valid_from=T0 - timedelta(days=3),
                expires_at=T0 + timedelta(days=30),
            ),
        ),
    )


def make_policy_proposal(
    trust_root: ResearchAuthorizationTrustRootV1,
    *,
    quest_id: str = QUEST_ID,
) -> ResearchAuthorizationPolicyProposalV1:
    keys = []
    for role, private_key in ROLE_PRIVATE_KEYS.items():
        public = ed25519_public_key_hex(private_key)
        keys.append(
            ResearchAuthorizationKey(
                key_id=ed25519_key_id(public),
                principal_id=ROLE_PRINCIPALS[role],
                role=role,
                public_key_ed25519_hex=public,
                valid_from=T0 - timedelta(days=1),
                expires_at=T0 + timedelta(days=10),
            )
        )
    return ResearchAuthorizationPolicyProposalV1(
        policy_id="rap_" + "3" * 32,
        quest_id=quest_id,
        trust_root_sha256=trust_root.trust_root_sha256,
        frozen_at=T0 - timedelta(hours=2),
        keys=tuple(sorted(keys, key=lambda item: item.key_id)),
    )


def make_policy(
    trust_root: ResearchAuthorizationTrustRootV1,
    *,
    root_private_key: bytes = ROOT_PRIVATE_KEY,
) -> ResearchAuthorizationPolicyV1:
    return certify_research_authorization_policy(
        make_policy_proposal(trust_root),
        trust_root=trust_root,
        root_key_id=trust_root.commissioning_keys[0].key_id,
        private_key=root_private_key,
        certified_at=T0 - timedelta(hours=1),
    )


def test_policy_is_content_addressed_and_root_certified() -> None:
    trust_root = make_trust_root()
    policy = make_policy(trust_root)

    verify_research_authorization_policy(policy=policy, trust_root=trust_root)
    assert policy.trust_root_sha256 == trust_root.trust_root_sha256
    assert len(policy.policy_sha256) == 64


def test_attacker_cannot_self_establish_a_policy_under_an_unrelated_root() -> None:
    trusted_root = make_trust_root()
    attacker_root = make_trust_root(private_key=b"\x55" * 32)
    attacker_policy = certify_research_authorization_policy(
        make_policy_proposal(attacker_root),
        trust_root=attacker_root,
        root_key_id=attacker_root.commissioning_keys[0].key_id,
        private_key=b"\x55" * 32,
        certified_at=T0 - timedelta(hours=1),
    )

    with pytest.raises(ResearchAuthorizationError, match="untrusted root"):
        verify_research_authorization_policy(
            policy=attacker_policy,
            trust_root=trusted_root,
        )


def test_policy_certificate_and_content_tampering_fail_closed() -> None:
    trust_root = make_trust_root()
    policy = make_policy(trust_root)
    tampered = policy.model_copy(update={"certification_signature_ed25519_hex": "0" * 128})

    with pytest.raises(ResearchAuthorizationError, match="certificate is invalid"):
        verify_research_authorization_policy(policy=tampered, trust_root=trust_root)
    with pytest.raises(ValidationError, match="proposal digest"):
        type(policy).model_validate(
            {**policy.model_dump(mode="python"), "quest_id": "qst_" + "9" * 32}
        )


def test_one_principal_cannot_span_disjoint_policy_roles() -> None:
    trust_root = make_trust_root()
    proposal = make_policy_proposal(trust_root)
    keys = list(proposal.keys)
    keys[1] = keys[1].model_copy(update={"principal_id": keys[0].principal_id})

    with pytest.raises(ValidationError, match="cannot span disjoint roles"):
        ResearchAuthorizationPolicyProposalV1.model_validate(
            {**proposal.model_dump(mode="python"), "keys": keys}
        )
