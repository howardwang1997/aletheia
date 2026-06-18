"""Phase 2 step 4: cross-model peer review — consensus rule, dynamic rebuttal
rounds (disagreement→more rounds, convergence→stop, cap honored), stance
assignment, and author-rebuttal invocation. All with stubbed providers (no network).
"""

from __future__ import annotations

import asyncio

from aletheia.config.settings import ConsensusConfig, CriticConfig, CriticsConfig, RoundsConfig
from aletheia.critics import gateway as gw_mod
from aletheia.critics import policy
from aletheia.critics.gateway import CriticGateway
from aletheia.critics.providers.base import CriticProvider
from aletheia.critics.schemas import CriticFinding, CriticResponse, Critique
from aletheia.db import create_all, session_scope
from aletheia.memory.ledger import CritiquePanel as CritiquePanelRow


class _Stub(CriticProvider):
    def __init__(self, vid, script):
        super().__init__(CriticConfig(id=vid, model="m"))
        self._script = script

    def review(self, instruction, content):  # sync, no network
        return self._script(content)


def _resp(verdict, blocker=False):
    findings = (
        [CriticFinding(severity="blocker", category="leakage", claim="x", evidence="y", suggestion="z")]
        if blocker else []
    )
    return CriticResponse(verdict=verdict, confidence=0.8, summary=verdict, findings=findings)


def _gw(providers, *, max_rounds=5, rule="any_blocker", dynamic=True):
    cfg = CriticsConfig(
        panel=[], consensus=ConsensusConfig(rule=rule),
        rounds=RoundsConfig(dynamic=dynamic, disagreement_threshold=0.34,
                            importance={"design": max_rounds, "default": 1}),
    )
    gw = CriticGateway(cfg)
    gw._providers = lambda *a, **k: providers  # inject stubs (accepts exclude_vendors)
    return gw


def _rounds_run(target_ref):
    with session_scope() as s:
        row = (
            s.query(CritiquePanelRow)
            .filter(CritiquePanelRow.target_ref == target_ref)
            .order_by(CritiquePanelRow.ts.desc())
            .first()
        )
        return len((row.raw_json or {}).get("rounds", []))


async def _fake_rebuttal(*args, **kwargs):
    return "[stub] rebuttal"


# --- stance assignment ---
def test_stance_assignment_distinct_models():
    provs = [_Stub(f"v{i}", lambda c: _resp("approve")) for i in range(3)]
    pairs = policy.assign_stances(provs)
    assert {st for _, st in pairs} == {"adversarial", "supportive"}
    assert len(pairs) == 3  # one stance per distinct model (no self-play)


def test_single_vendor_degrades_to_both_stances():
    pairs = policy.assign_stances([_Stub("solo", lambda c: _resp("approve"))])
    assert [st for _, st in pairs] == ["adversarial", "supportive"]


# --- consensus rule ---
def test_consensus_any_blocker_vs_majority():
    crit = [
        Critique(critic_id="a", stance="adversarial", **_resp("reject", blocker=True).model_dump()),
        Critique(critic_id="b", stance="supportive", **_resp("approve").model_dump()),
        Critique(critic_id="c", stance="supportive", **_resp("approve").model_dump()),
    ]
    assert _gw([])._consensus(crit) == ("reject", False)  # any_blocker: one blocker fails
    verdict, gate = _gw([], rule="majority")._consensus(crit)
    assert gate is True and verdict == "approve_with_changes"  # majority approves


# --- dynamic rounds ---
def test_rounds_converge_after_rebuttal(monkeypatch):
    create_all()
    monkeypatch.setattr(gw_mod, "run_worker", _fake_rebuttal)

    def adv(content):
        return _resp("approve_with_changes") if "AUTHOR REBUTTAL" in content else _resp("reject", blocker=True)

    gw = _gw([_Stub("adv", adv), _Stub("sup", lambda c: _resp("approve"))], max_rounds=5)
    panel = asyncio.run(gw.review("design", {"x": 1}, "exp-conv", dry_run=False))
    assert _rounds_run("exp-conv") == 2  # round 1 disagrees, round 2 converges
    assert panel.gate_passed is True
    assert panel.consensus_verdict == "approve_with_changes"


def test_unanimous_stops_in_one_round(monkeypatch):
    create_all()
    monkeypatch.setattr(gw_mod, "run_worker", _fake_rebuttal)
    gw = _gw([_Stub("a", lambda c: _resp("approve")), _Stub("b", lambda c: _resp("approve"))], max_rounds=5)
    panel = asyncio.run(gw.review("design", {"x": 1}, "exp-unan", dry_run=False))
    assert _rounds_run("exp-unan") == 1
    assert panel.gate_passed is True


def test_cap_honored_on_persistent_disagreement(monkeypatch):
    create_all()
    calls = {"n": 0}

    async def counting_rebuttal(*a, **k):
        calls["n"] += 1
        return "rebuttal"

    monkeypatch.setattr(gw_mod, "run_worker", counting_rebuttal)
    gw = _gw(
        [_Stub("adv", lambda c: _resp("reject", blocker=True)), _Stub("sup", lambda c: _resp("approve"))],
        max_rounds=3,
    )
    panel = asyncio.run(gw.review("design", {"x": 1}, "exp-cap", dry_run=False))
    assert _rounds_run("exp-cap") == 3  # cap honored
    assert calls["n"] == 2  # author rebuttal runs between rounds, not after the last
    assert panel.gate_passed is False  # unrefuted blocker survives to the final round


# --- vendor-failure visibility (a dropped auditor must be diagnosable, not silent) ---
def test_failing_vendor_is_dropped_and_evented():
    # a vendor that errors (e.g. unreachable on a direct link) is dropped from the panel — not fatal —
    # but the drop must be VISIBLE so a starved audit (n_auditors < min_vendors) is diagnosable
    # instead of guessed. Emit one critic_vendor_error per drop, naming the vendor + error.
    create_all()
    good = _Stub("grok", lambda c: _resp("approve"))

    def _boom(_content):
        raise RuntimeError("ECONNRESET (simulated direct-link drop)")

    bad = _Stub("deepseek", _boom)
    gw = _gw([good, bad])
    assignments = [(good, "supportive"), (bad, "adversarial")]
    critiques = asyncio.run(
        gw._run_round_async("demonstration_audit", "{}", assignments, run_id="run-vdrop")
    )
    assert {c.critic_id for c in critiques} == {"grok"}  # failing vendor dropped, not fatal

    from aletheia.memory.ledger import Event

    with session_scope() as s:
        rows = (
            s.query(Event.payload)
            .filter(Event.run_id == "run-vdrop", Event.type == "critic_vendor_error")
            .all()
        )
    payloads = [r[0] for r in rows]
    assert any(
        p.get("vendor") == "deepseek" and "ECONNRESET" in (p.get("error") or "")
        for p in payloads
    )
