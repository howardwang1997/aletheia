"""Candidate-envelope validation and objective information accounting."""

from __future__ import annotations

import pytest

from aletheia.evals.adapters import discoveryworld_server_entrypoint as server
from aletheia.evals.adapters.discoveryworld_server_entrypoint import (
    _read_json,
    _validate_envelope,
)


UNIFORM = {
    "substance_a": 0.25,
    "substance_b": 0.25,
    "substance_c": 0.25,
    "substance_d": 0.25,
}


def test_valid_act_and_stop_envelopes_are_normalized():
    act = _validate_envelope(
        {
            "schema_version": 1,
            "sequence": 0,
            "kind": "act",
            "world_action": {"action": "MOVE_DIRECTION", "arg1": "north"},
            "beliefs": UNIFORM,
        },
        0,
    )
    assert act["world_action"]["action"] == "MOVE_DIRECTION"
    stop = _validate_envelope(
        {
            "schema_version": 1,
            "sequence": 1,
            "kind": "stop",
            "final_hypothesis_id": "substance_b",
            "beliefs": {**UNIFORM, "substance_b": 0.25},
        },
        1,
    )
    assert stop["final_hypothesis_id"] == "substance_b"


@pytest.mark.parametrize(
    "payload,match",
    [
        (
            {
                "schema_version": 1,
                "sequence": 1,
                "kind": "act",
                "world_action": {"action": "WAIT"},
                "beliefs": UNIFORM,
            },
            "identity",
        ),
        (
            {
                "schema_version": 1,
                "sequence": 0,
                "kind": "act",
                "world_action": {"action": "WAIT", "oracle": True},
                "beliefs": UNIFORM,
            },
            "outside",
        ),
        (
            {
                "schema_version": 1,
                "sequence": 0,
                "kind": "act",
                "world_action": {"action": "WAIT"},
                "beliefs": {**UNIFORM, "substance_a": 0.5},
            },
            "sum",
        ),
        (
            {
                "schema_version": 1,
                "sequence": 0,
                "kind": "stop",
                "final_hypothesis_id": "oracle",
                "beliefs": UNIFORM,
            },
            "final_hypothesis_id",
        ),
    ],
)
def test_malformed_or_oracle_envelopes_fail_closed(payload, match):
    with pytest.raises(ValueError, match=match):
        _validate_envelope(payload, 0)


def test_protocol_reader_rejects_links_and_oversized_files(tmp_path):
    valid = tmp_path / "valid.json"
    valid.write_text('{"schema_version": 1}', encoding="utf-8")
    assert _read_json(valid, max_bytes=64) == {"schema_version": 1}

    link = tmp_path / "link.json"
    link.symlink_to(valid)
    with pytest.raises(ValueError, match="non-symlink"):
        _read_json(link, max_bytes=64)

    oversized = tmp_path / "oversized.json"
    oversized.write_text('{"padding":"' + ("x" * 80) + '"}', encoding="utf-8")
    with pytest.raises(ValueError, match="byte limit"):
        _read_json(oversized, max_bytes=64)


def test_trusted_server_exits_immediately_after_terminal_receipt(monkeypatch):
    events = []
    monkeypatch.setenv("DW_MODE", "freeze")
    monkeypatch.setattr(server, "_freeze", lambda: events.append("receipt"))
    monkeypatch.setattr(server.os, "_exit", lambda code: events.append(("exit", code)))

    server.main()

    assert events == ["receipt", ("exit", 0)]
