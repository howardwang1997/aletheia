"""Trusted, stdlib-only DiscoveryWorld episode server.

This file is mounted only into the evaluator-owned environment container.  Candidate programs
communicate through two one-way directories: observations are read-only to candidates and actions
are read-only to this server.  The complete scorecard, parametric seed, governing rule, and result
receipt never enter the candidate container.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any


HYPOTHESES = (
    ("substance_a", "Pure Substance A is the rust remover."),
    ("substance_b", "Pure Substance B is the rust remover."),
    ("substance_c", "Pure Substance C is the rust remover."),
    ("substance_d", "Pure Substance D is the rust remover."),
)
HYPOTHESIS_IDS = tuple(item[0] for item in HYPOTHESES)
SUBSTANCE_TO_ID = {
    "Substance A": "substance_a",
    "Substance B": "substance_b",
    "Substance C": "substance_c",
    "Substance D": "substance_d",
}
MAX_ACTION_BYTES = 65_536
MAX_NOTE_CHARS = 2_000


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_bytes(payload))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_json(path: Path, *, max_bytes: int) -> Any:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("protocol file must be a regular non-symlink file") from exc
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("protocol file must be a regular non-symlink file")
        if metadata.st_size > max_bytes:
            raise ValueError("protocol file exceeds its byte limit")
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("protocol file exceeds its byte limit")
    return json.loads(
        raw,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON number: {value}")
        ),
    )


def _load_world(scenario: str, difficulty: str, seed: int):
    # The evaluator image sets SDL_VIDEODRIVER=dummy.  DiscoveryWorld still initializes its
    # official sprite/UI stack once, but observations below use renderJSON() and never write
    # screenshots or expose vision bytes to the candidate.
    from discoveryworld.DiscoveryWorldAPI import DiscoveryWorldAPI
    from discoveryworld.World import World

    # The upstream tick records a compressed pickle of all ~1,100 world objects after every
    # action. That history is used by the optional natural-language knowledge scorer, which this
    # adapter deliberately replaces with a structured finite-rule trace. It is not read by world
    # dynamics, the task scorer, renderJSON(), or our receipt. Disabling this observational side
    # log leaves official actions/task transitions intact and avoids an unbounded duplicate trace.
    World.saveWorldHistory = lambda _world: None

    api = DiscoveryWorldAPI(threadID=(seed + 1) * 1009)
    if not api.loadScenario(
        scenarioName=scenario,
        difficultyStr=difficulty,
        randomSeed=seed,
        numUserAgents=1,
    ):
        raise RuntimeError("official DiscoveryWorld rejected the frozen scenario")
    if len(api.world.taskScorer.tasks) != 1:
        raise RuntimeError("frozen mini-suite requires exactly one official task")
    task = api.world.taskScorer.tasks[0]
    if task.taskName != "RustedKeyTaskEasy":
        raise RuntimeError("frozen mini-suite loaded an unexpected official task")
    return api, task


def _text_observation(api) -> dict[str, Any]:
    ui = api.ui[0].renderJSON()
    api.taskProgress = ui["taskProgress"]
    api.steps = ui["world_steps"]
    return {"errors": [], "ui": ui}


def _world_contract(api, task) -> dict[str, Any]:
    solution = task.scoringInfo.get("chemicalSolutionDict")
    if not isinstance(solution, dict) or len(solution) != 1:
        raise RuntimeError("easy chemistry task no longer has one governing substance")
    substance, amount = next(iter(solution.items()))
    if substance not in SUBSTANCE_TO_ID or float(amount) != 1.0:
        raise RuntimeError("easy chemistry governing rule left the frozen hypothesis space")
    if len(task.criticalHypotheses) != 1 or len(task.criticalQuestions) != 1:
        raise RuntimeError("official explanatory-knowledge contract changed")
    known_actions = api.listKnownActions(limited=False)
    teleports = api.listTeleportLocationsDict()
    initial_observation = _text_observation(api)
    return {
        "task_name": task.taskName,
        "task_description": task.taskDescription,
        "hypothesis_space": [
            {"hypothesis_id": hypothesis_id, "claim": claim} for hypothesis_id, claim in HYPOTHESES
        ],
        "correct_hypothesis_id": SUBSTANCE_TO_ID[substance],
        "critical_hypothesis_sha256": hashlib.sha256(
            task.criticalHypotheses[0].encode("utf-8")
        ).hexdigest(),
        "critical_question_sha256": hashlib.sha256(
            task.criticalQuestions[0].encode("utf-8")
        ).hexdigest(),
        "known_actions_sha256": _sha256(known_actions),
        "teleport_locations_sha256": _sha256(teleports),
        "initial_observation_sha256": _sha256(initial_observation),
        "known_actions": known_actions,
        "teleport_locations": teleports,
        "initial_observation": initial_observation,
    }


def _freeze() -> None:
    scenario = os.environ["DW_SCENARIO"]
    difficulty = os.environ["DW_DIFFICULTY"]
    seed = int(os.environ["DW_WORLD_SEED"])
    api, task = _load_world(scenario, difficulty, seed)
    contract = _world_contract(api, task)
    payload = {
        "schema_version": 1,
        "source_manifest_sha256": os.environ["DW_SOURCE_MANIFEST_SHA256"],
        "instance_id": os.environ["DW_INSTANCE_ID"],
        "scenario": scenario,
        "difficulty": difficulty,
        "world_seed": seed,
        **{
            key: value
            for key, value in contract.items()
            if key not in {"known_actions", "teleport_locations", "initial_observation"}
        },
    }
    _atomic_json(Path(os.environ["DW_RESULT_PATH"]), payload)


def _validate_beliefs(value: Any) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != set(HYPOTHESIS_IDS):
        raise ValueError("beliefs must contain exactly the frozen hypothesis IDs")
    beliefs: dict[str, float] = {}
    for key in HYPOTHESIS_IDS:
        probability = value[key]
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise ValueError("belief probabilities must be numbers")
        probability = float(probability)
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError("belief probabilities must be finite values in [0,1]")
        beliefs[key] = probability
    if abs(sum(beliefs.values()) - 1.0) > 1e-6:
        raise ValueError("belief probabilities must sum to one")
    return beliefs


def _validate_envelope(value: Any, sequence: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("action envelope must be an object")
    common = {"schema_version", "sequence", "kind", "beliefs", "hypothesis_note"}
    if value.get("schema_version") != 1 or value.get("sequence") != sequence:
        raise ValueError("action envelope identity does not match the requested sequence")
    kind = value.get("kind")
    if kind not in {"act", "stop"}:
        raise ValueError("action envelope kind must be act or stop")
    allowed = common | ({"world_action"} if kind == "act" else {"final_hypothesis_id"})
    if set(value) - allowed:
        raise ValueError("action envelope contains undeclared fields")
    note = value.get("hypothesis_note", "")
    if not isinstance(note, str) or len(note) > MAX_NOTE_CHARS:
        raise ValueError("hypothesis_note must be bounded UTF-8 text")
    beliefs = _validate_beliefs(value.get("beliefs"))
    if kind == "stop":
        final_id = value.get("final_hypothesis_id")
        if final_id not in HYPOTHESIS_IDS:
            raise ValueError("stop requires one frozen final_hypothesis_id")
        return {
            "schema_version": 1,
            "sequence": sequence,
            "kind": kind,
            "beliefs": beliefs,
            "hypothesis_note": note,
            "final_hypothesis_id": final_id,
        }
    world_action = value.get("world_action")
    if not isinstance(world_action, dict) or not world_action:
        raise ValueError("act requires a non-empty world_action object")
    if set(world_action) - {"action", "arg1", "arg2"}:
        raise ValueError("world_action contains fields outside the official JSON API")
    action_name = world_action.get("action")
    if not isinstance(action_name, str) or not 1 <= len(action_name) <= 64:
        raise ValueError("world_action.action must be bounded text")
    for key in ("arg1", "arg2"):
        if key in world_action and (
            isinstance(world_action[key], (dict, list))
            or not isinstance(world_action[key], (str, int, float, bool, type(None)))
        ):
            raise ValueError("world action arguments must be scalar JSON values")
    return {
        "schema_version": 1,
        "sequence": sequence,
        "kind": kind,
        "beliefs": beliefs,
        "hypothesis_note": note,
        "world_action": world_action,
    }


def _scorecard(task) -> dict[str, Any]:
    return {
        "task_name": task.taskName,
        "completed": bool(task.completed),
        "completed_successfully": bool(task.completedSuccessfully),
        "score_normalized": float(task.getScoreNormalized()),
    }


def _run_episode() -> None:
    hidden = _read_json(Path(os.environ["DW_HIDDEN_CONTRACT"]), max_bytes=1 << 20)
    scenario = str(hidden["scenario"])
    difficulty = str(hidden["difficulty"])
    seed = int(hidden["world_seed"])
    max_actions = int(os.environ["DW_MAX_ACTIONS"])
    action_wait_s = float(os.environ["DW_ACTION_WAIT_S"])
    if not 1 <= max_actions <= 500 or not 0 < action_wait_s <= 3600:
        raise RuntimeError("episode resource contract is invalid")

    actions_dir = Path(os.environ["DW_ACTIONS_DIR"])
    observations_dir = Path(os.environ["DW_OBSERVATIONS_DIR"])
    result_path = Path(os.environ["DW_RESULT_PATH"])
    exit_sentinel = actions_dir / "candidate_exit.json"
    api, task = _load_world(scenario, difficulty, seed)
    contract = _world_contract(api, task)
    for key in (
        "task_name",
        "correct_hypothesis_id",
        "critical_hypothesis_sha256",
        "critical_question_sha256",
        "known_actions_sha256",
        "teleport_locations_sha256",
        "initial_observation_sha256",
    ):
        if hidden.get(key) != contract[key]:
            raise RuntimeError(f"runtime DiscoveryWorld contract drifted at {key}")

    scoring = task.scoringInfo
    jar_uuid = scoring["mixingJar"].uuid
    key_uuid = scoring["key"].uuid
    cleaner_uuid = scoring["bottleCleaner"].uuid
    dispenser_by_uuid = {
        dispenser.uuid: SUBSTANCE_TO_ID[
            dispenser.name.removeprefix("Dispenser (").removesuffix(")")
        ]
        for dispenser in scoring["dispensers"]
    }
    current_measures: list[str] = []
    pending_trial: dict[str, Any] | None = None
    objective_remaining = set(HYPOTHESIS_IDS)
    tested_hypotheses: set[str] = set()
    trace: list[dict[str, Any]] = []
    protocol_valid = True
    terminal_reason = "candidate_exited"
    stopped = False
    final_hypothesis_id: str | None = None
    action_count = 0
    valid_action_count = 0
    invalid_action_count = 0
    world_terminal_reason: str | None = None

    initial_packet = {
        "schema_version": 1,
        "sequence": 0,
        "terminal": False,
        "observation": contract["initial_observation"],
        "action_result": None,
        "known_actions": contract["known_actions"],
        "teleport_locations": contract["teleport_locations"],
        "hypothesis_space": contract["hypothesis_space"],
        "protocol": {
            "action_filename": "action_{sequence:04d}.json",
            "beliefs_required_every_turn": True,
            "stop_requires_final_hypothesis": True,
            "max_world_actions": max_actions,
        },
    }
    observation_before = initial_packet
    _atomic_json(observations_dir / "observation_0000.json", initial_packet)
    _atomic_json(observations_dir / "ready.json", {"schema_version": 1, "ready": True})

    sequence = 0
    while sequence <= max_actions:
        action_path = actions_dir / f"action_{sequence:04d}.json"
        deadline = time.monotonic() + action_wait_s
        while not action_path.exists():
            if exit_sentinel.exists():
                terminal_reason = "candidate_exited"
                break
            if time.monotonic() >= deadline:
                terminal_reason = "action_wait_limit"
                break
            time.sleep(0.02)
        if not action_path.exists():
            break
        try:
            envelope = _validate_envelope(
                _read_json(action_path, max_bytes=MAX_ACTION_BYTES), sequence
            )
        except Exception:
            protocol_valid = False
            terminal_reason = "protocol_breach"
            break

        note_hash = hashlib.sha256(envelope["hypothesis_note"].encode("utf-8")).hexdigest()
        observation_before_hash = _sha256(observation_before)
        if envelope["kind"] == "stop":
            stopped = True
            final_hypothesis_id = envelope["final_hypothesis_id"]
            terminal_reason = "candidate_stopped"
            trace.append(
                {
                    "sequence": sequence,
                    "kind": "stop",
                    "world_action": None,
                    "action_sha256": _sha256(envelope),
                    "observation_before_sha256": observation_before_hash,
                    "observation_after_sha256": None,
                    "valid_action": None,
                    "world_step_before": int(api.world.getStepCounter()),
                    "world_step_after": int(api.world.getStepCounter()),
                    "beliefs": envelope["beliefs"],
                    "hypothesis_note_sha256": note_hash,
                    "informative_trial_hypothesis_id": None,
                    "informative_trial_outcome": None,
                    "objective_remaining_after": sorted(objective_remaining),
                }
            )
            break

        if world_terminal_reason is not None:
            # Once the official task or action budget is terminal, the policy gets one final
            # observation so it can state its structured conclusion.  Further world actions are
            # an authored protocol violation; only ``stop`` is legal from that state.
            protocol_valid = False
            terminal_reason = "protocol_breach"
            break
        if sequence >= max_actions:
            terminal_reason = "world_action_limit"
            break
        action_count += 1
        world_action = envelope["world_action"]
        world_step_before = int(api.world.getStepCounter())
        try:
            action_result = api.performAgentAction(agentIdx=0, actionJSON=world_action)
            if not isinstance(action_result, dict):
                action_result = {"errors": ["official API returned no result"], "success": False}
        except Exception:
            # Malformed-but-schema-valid official actions are authored scientific failures, not
            # evaluator infrastructure failures.  Do not expose upstream exception text.
            action_result = {"errors": ["official API rejected the action"], "success": False}
        action_success = bool(action_result.get("success") is True)
        if action_success:
            valid_action_count += 1
        else:
            invalid_action_count += 1

        action_name = world_action.get("action")
        arg1 = world_action.get("arg1")
        arg2 = world_action.get("arg2")
        if action_success and action_name == "USE" and arg2 == jar_uuid:
            if arg1 in dispenser_by_uuid:
                current_measures.append(dispenser_by_uuid[arg1])
            elif arg1 == cleaner_uuid:
                current_measures = []
        if (
            action_success
            and action_name == "PUT"
            and arg1 == key_uuid
            and arg2 == jar_uuid
            and current_measures
            and len(set(current_measures)) == 1
        ):
            pending_trial = {"hypothesis_id": current_measures[0], "age": 0}

        api.tick()
        trial_id: str | None = None
        trial_outcome: str | None = None
        if pending_trial is not None:
            pending_trial["age"] += 1
            key_is_rusted = bool(scoring["key"].attributes["isRusted"])
            if not key_is_rusted or pending_trial["age"] >= 2:
                trial_id = str(pending_trial["hypothesis_id"])
                trial_outcome = "positive" if not key_is_rusted else "negative"
                trial_was_new = trial_id not in tested_hypotheses
                tested_hypotheses.add(trial_id)
                if trial_outcome == "positive":
                    objective_remaining = {trial_id}
                else:
                    objective_remaining.discard(trial_id)
                    if not objective_remaining:
                        raise RuntimeError("objective hypothesis set became empty")
                pending_trial = None

        observation = _text_observation(api)
        terminal = bool(task.completedSuccessfully) or action_count >= max_actions
        packet = {
            "schema_version": 1,
            "sequence": sequence + 1,
            "terminal": terminal,
            "observation": observation,
            "action_result": {
                "success": action_success,
                "errors": [str(item)[:512] for item in action_result.get("errors", [])[:8]],
            },
            "experiment_receipt": (
                {
                    "hypothesis_id": trial_id,
                    "outcome": trial_outcome,
                    "new_information": trial_was_new,
                }
                if trial_id is not None
                else None
            ),
        }
        observation_after_hash = _sha256(packet)
        trace.append(
            {
                "sequence": sequence,
                "kind": "act",
                "world_action": world_action,
                "action_sha256": _sha256(envelope),
                "observation_before_sha256": observation_before_hash,
                "observation_after_sha256": observation_after_hash,
                "valid_action": action_success,
                "world_step_before": world_step_before,
                "world_step_after": int(api.world.getStepCounter()),
                "beliefs": envelope["beliefs"],
                "hypothesis_note_sha256": note_hash,
                "informative_trial_hypothesis_id": trial_id,
                "informative_trial_outcome": trial_outcome,
                "objective_remaining_after": sorted(objective_remaining),
            }
        )
        _atomic_json(observations_dir / f"observation_{sequence + 1:04d}.json", packet)
        observation_before = packet
        sequence += 1
        if terminal:
            world_terminal_reason = (
                "official_task_complete" if task.completedSuccessfully else "world_action_limit"
            )
            terminal_reason = world_terminal_reason

    scorecard = _scorecard(task)
    receipt = {
        "schema_version": 1,
        "source_manifest_sha256": hidden["source_manifest_sha256"],
        "instance_id": hidden["instance_id"],
        "scenario": scenario,
        "difficulty": difficulty,
        "world_seed": seed,
        "task_name": task.taskName,
        "critical_hypothesis_sha256": contract["critical_hypothesis_sha256"],
        "correct_hypothesis_id": contract["correct_hypothesis_id"],
        "protocol_valid": protocol_valid,
        "terminal_reason": terminal_reason,
        "stopped": stopped,
        "final_hypothesis_id": final_hypothesis_id,
        **scorecard,
        "action_count": action_count,
        "valid_action_count": valid_action_count,
        "invalid_action_count": invalid_action_count,
        "tested_hypothesis_ids": sorted(tested_hypotheses),
        "objective_remaining": sorted(objective_remaining),
        "trace": trace,
        "trace_sha256": _sha256(trace),
    }
    _atomic_json(result_path, receipt)


def main() -> None:
    mode = os.environ.get("DW_MODE")
    if mode == "freeze":
        _freeze()
    elif mode == "episode":
        _run_episode()
    else:
        raise RuntimeError("DW_MODE must be freeze or episode")

    # The atomic, fsynced evaluator receipt is the terminal commit for this one-shot process.
    # Exit without running third-party interpreter teardown: pygame/SDL shutdown has
    # intermittently left a processless container reported as running by Docker-on-Colima even
    # though the complete receipt was already visible on the evaluator-only bind mount.  This is
    # environment lifecycle hardening only; exceptions above still take the normal non-zero path.
    os._exit(0)


if __name__ == "__main__":
    main()
