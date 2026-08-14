"""Real two-container proof for the DiscoveryWorld hidden-rule adapter."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aletheia.config import get_settings
from aletheia.evals.adapters.discoveryworld import (
    DISCOVERYWORLD_COMMIT,
    DISCOVERYWORLD_SOURCE_ARCHIVE_SHA256,
    DiscoveryWorldAdapter,
    DiscoveryWorldInstanceSpec,
    DiscoveryWorldScorer,
    DiscoveryWorldSourceManifest,
    DockerDiscoveryWorldHarness,
)
from aletheia.evals.schemas import (
    EvaluationSubmission,
    InvalidReason,
    ResourceBudget,
    SubmittedArtifact,
)
from aletheia.paths import WORKSPACES_ROOT

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module", autouse=True)
def _images_available():
    settings = get_settings()
    for image in (
        settings.discoveryworld_candidate_docker_image,
        settings.discoveryworld_docker_image,
    ):
        inspect = subprocess.run(
            [settings.sandbox_docker_command, "image", "inspect", image],
            capture_output=True,
            text=True,
            check=False,
        )
        if inspect.returncode != 0:
            pytest.skip(f"DiscoveryWorld image unavailable: {image}")


@pytest.fixture(scope="module")
def evaluator_bundle():
    path = Path(WORKSPACES_ROOT) / ".eval_test_tmp" / f"discoveryworld-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield build_evaluator(path)
    finally:
        os.chmod(path, 0o700)
        shutil.rmtree(path, ignore_errors=True)


def build_evaluator(workspace: Path):
    source = DiscoveryWorldSourceManifest.official_public_validation()
    adapter = DiscoveryWorldAdapter(source)
    settings = get_settings()
    harness = DockerDiscoveryWorldHarness.from_image_refs(
        candidate_image_ref=settings.discoveryworld_candidate_docker_image,
        environment_image_ref=settings.discoveryworld_docker_image,
        source_manifest_sha256=source.manifest_sha256,
        scratch_root=workspace / "scratch",
        reproduction_runs=2,
        max_world_actions=40,
        candidate_wall_time_s=45,
        candidate_cpu_seconds=30,
        candidate_memory_mb=256,
        # Match the production freeze budget. DiscoveryWorld initializes its complete official
        # sprite/object stack; aggregate Docker CI can briefly contend even though normal startup
        # takes only a few seconds. Candidate episodes are still never retried or selected.
        environment_wall_time_s=150,
        environment_cpu_seconds=120,
        environment_memory_mb=2048,
        ready_wait_s=45,
        action_wait_s=50,
    )
    receipt = harness.freeze_instance(
        spec=DiscoveryWorldInstanceSpec(instance_id="chem-easy-docker", world_seed=0)
    )
    scorer = DiscoveryWorldScorer(harness=harness, source_manifest_sha256=source.manifest_sha256)
    task = adapter.build_task(
        receipt=receipt,
        scorer=scorer,
        resource_budget=ResourceBudget(wall_time_s=180, cpu_seconds=120, memory_mb=1024),
    )
    return source, receipt, harness, scorer, task


def score_program(*, scorer, task, receipt, program: bytes):
    artifact = SubmittedArtifact(
        kind="agent_program",
        media_type="text/x-python",
        uri="inbox://agent.py",
        sha256=hashlib.sha256(program).hexdigest(),
        bytes=len(program),
    )
    submission = EvaluationSubmission(
        attempt_id="discoveryworld-docker-attempt",
        task_manifest_sha256=task.manifest_sha256,
        system_manifest_sha256="a" * 64,
        artifacts=(artifact,),
        submitted_at=datetime.now(timezone.utc),
    )
    return scorer.score(
        task=task,
        hidden_asset=receipt.to_bytes(),
        submission=submission,
        artifacts={"agent_program": program},
    )


STOP_ONLY_POLICY = rb"""
import importlib.util
import json
import os
import time
from pathlib import Path

assert importlib.util.find_spec("discoveryworld") is None
assert not Path("/opt/discoveryworld-source").exists()
assert not Path("/opt/aletheia-discoveryworld-server.py").exists()
observations = Path(os.environ["DISCOVERYWORLD_OBSERVATIONS_DIR"])
actions = Path(os.environ["DISCOVERYWORLD_ACTIONS_DIR"])
deadline = time.monotonic() + 20
observation = observations / "observation_0000.json"
while not observation.is_file():
    assert time.monotonic() < deadline
    time.sleep(0.01)
packet = json.loads(observation.read_text())
assert packet["sequence"] == 0
payload = {
    "schema_version": 1,
    "sequence": 0,
    "kind": "stop",
    "final_hypothesis_id": "substance_a",
    "beliefs": {
        "substance_a": 0.25,
        "substance_b": 0.25,
        "substance_c": 0.25,
        "substance_d": 0.25,
    },
    "hypothesis_note": "No experiment was run.",
}
temporary = actions / ".action_0000.json.tmp"
temporary.write_text(json.dumps(payload, sort_keys=True))
os.replace(temporary, actions / "action_0000.json")
"""


SYSTEMATIC_SCIENTIST_POLICY = rb"""
import json
import os
import time
from pathlib import Path

observations = Path(os.environ["DISCOVERYWORLD_OBSERVATIONS_DIR"])
actions = Path(os.environ["DISCOVERYWORLD_ACTIONS_DIR"])
sequence = 0

def read_observation(number):
    path = observations / f"observation_{number:04d}.json"
    deadline = time.monotonic() + 30
    while not path.is_file():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"observation {number} did not arrive")
        time.sleep(0.01)
    return json.loads(path.read_text())

def submit(kind, beliefs, note, **fields):
    global sequence
    payload = {
        "schema_version": 1,
        "sequence": sequence,
        "kind": kind,
        "beliefs": beliefs,
        "hypothesis_note": note,
        **fields,
    }
    destination = actions / f"action_{sequence:04d}.json"
    temporary = actions / f".action_{sequence:04d}.tmp"
    temporary.write_text(json.dumps(payload, sort_keys=True))
    os.replace(temporary, destination)
    if kind == "stop":
        return None
    sequence += 1
    return read_observation(sequence)

def act(world_action, beliefs, note):
    return submit("act", beliefs, note, world_action=world_action)

def visible_objects(packet):
    ui = packet["observation"]["ui"]
    objects = list(ui["inventoryObjects"]) + list(ui["accessibleEnvironmentObjects"])
    for values in ui["nearbyObjects"]["objects"].values():
        objects.extend(values)
    return objects

def find(packet, predicate):
    matches = [item for item in visible_objects(packet) if predicate(item)]
    if not matches:
        raise RuntimeError("required public object is not visible")
    return matches[0]["uuid"]

uniform = {key: 0.25 for key in (
    "substance_a", "substance_b", "substance_c", "substance_d"
)}
packet = read_observation(0)
jar = find(packet, lambda item: item["name"] == "jar")
key = find(packet, lambda item: "key" in item["name"])
door = find(packet, lambda item: item["name"] == "door")
dispensers = {
    letter: find(packet, lambda item, letter=letter: item["name"] == f"Dispenser (Substance {letter})")
    for letter in "ABCD"
}

# The cleaner is just beyond the first observation radius. Teleporting to an already observed
# dispenser is an ordinary public navigation action and brings it into view.
packet = act(
    {"action": "TELEPORT_TO_OBJECT", "arg1": dispensers["D"]},
    uniform,
    "Survey the apparatus before intervening.",
)
cleaner = find(
    packet,
    lambda item: "bottle" in item["name"].lower() and "clean" in item["name"].lower(),
)
packet = act(
    {"action": "TELEPORT_TO_OBJECT", "arg1": jar}, uniform, "Approach the mixing jar."
)
packet = act({"action": "PICKUP", "arg1": jar}, uniform, "Take the empty jar.")
packet = act(
    {"action": "TELEPORT_TO_OBJECT", "arg1": key}, uniform, "Approach the rusted key."
)
packet = act({"action": "PICKUP", "arg1": key}, uniform, "Take the rusted key.")

remaining = ["substance_a", "substance_b", "substance_c", "substance_d"]
letters = {
    "substance_a": "A", "substance_b": "B", "substance_c": "C", "substance_d": "D"
}
discovered = None
for hypothesis_id in list(remaining):
    alternatives = [item for item in remaining if item != hypothesis_id]
    beliefs = {item: 0.0 for item in letters}
    beliefs[hypothesis_id] = 0.7 if alternatives else 1.0
    for item in alternatives:
        beliefs[item] = (1.0 - beliefs[hypothesis_id]) / len(alternatives)
    packet = act(
        {"action": "TELEPORT_TO_OBJECT", "arg1": dispensers[letters[hypothesis_id]]},
        beliefs,
        f"Prepare a pure test of {hypothesis_id}.",
    )
    packet = act(
        {"action": "USE", "arg1": dispensers[letters[hypothesis_id]], "arg2": jar},
        beliefs,
        f"Dispense only {hypothesis_id} into the clean jar.",
    )
    packet = act(
        {"action": "PUT", "arg1": key, "arg2": jar},
        beliefs,
        f"Expose the rusted key to pure {hypothesis_id}.",
    )
    experiment = packet.get("experiment_receipt")
    if experiment is None:
        packet = act(
            {"action": "ROTATE_CW"}, beliefs, "Wait one controlled world step for the reaction."
        )
        experiment = packet.get("experiment_receipt")
    if not experiment or experiment["hypothesis_id"] != hypothesis_id:
        raise RuntimeError("trusted experiment receipt was not issued")
    if experiment["outcome"] == "positive":
        discovered = hypothesis_id
        beliefs = {item: float(item == discovered) for item in letters}
        break
    remaining.remove(hypothesis_id)
    beliefs = {item: (1.0 / len(remaining) if item in remaining else 0.0) for item in letters}
    packet = act(
        {"action": "PICKUP", "arg1": key},
        beliefs,
        f"Reject {hypothesis_id}; retrieve the still-rusted key.",
    )
    packet = act(
        {"action": "TELEPORT_TO_OBJECT", "arg1": cleaner},
        beliefs,
        "Approach the bottle cleaner to reset the apparatus.",
    )
    packet = act(
        {"action": "USE", "arg1": cleaner, "arg2": jar},
        beliefs,
        "Clean the jar before the next controlled trial.",
    )

if discovered is None:
    raise RuntimeError("finite hypothesis search ended without a positive trial")
packet = act(
    {"action": "PICKUP", "arg1": key}, beliefs, "Retrieve the experimentally derusted key."
)
packet = act(
    {"action": "TELEPORT_TO_OBJECT", "arg1": door}, beliefs, "Approach the locked shed door."
)
packet = act({"action": "OPEN", "arg1": door}, beliefs, "Open the door with the derusted key.")
packet = act({"action": "MOVE_DIRECTION", "arg1": "south"}, beliefs, "Move through the door.")
packet = act({"action": "MOVE_DIRECTION", "arg1": "south"}, beliefs, "Leave the shed.")
if not packet["terminal"]:
    raise RuntimeError("official task did not reach its terminal state")
submit(
    "stop",
    beliefs,
    f"Controlled pure-substance trials identify {discovered} as the governing rule.",
    final_hypothesis_id=discovered,
)
"""


def test_real_images_are_frozen_and_candidate_is_neutral(evaluator_bundle):
    source, _receipt, harness, _scorer, _task = evaluator_bundle
    assert source.repository_commit == DISCOVERYWORLD_COMMIT
    assert source.source_archive_sha256 == DISCOVERYWORLD_SOURCE_ARCHIVE_SHA256
    assert harness.manifest.candidate_image_id != harness.manifest.environment_image_id
    assert harness.manifest.candidate_environment["discoveryworld"] == "not-installed"
    assert harness.manifest.candidate_environment["aletheia_source"] == "absent"
    assert harness.manifest.discoveryworld_environment["discoveryworld"] == "0.0.2"
    assert harness.manifest.world_history_policy == (
        "disabled-replaced-by-authoritative-action-trace"
    )
    assert harness.manifest.trusted_server_exit_policy == (
        "atomic-fsynced-receipt-then-immediate-process-exit"
    )
    assert harness.manifest.candidate_terminal_policy == (
        "validated-stop-receipt-terminates-candidate"
    )
    assert harness.manifest.network_mode == "none"


def test_real_systematic_experiment_discovers_rule_and_completes_task(evaluator_bundle):
    _source, receipt, _harness, scorer, task = evaluator_bundle
    score = score_program(
        scorer=scorer,
        task=task,
        receipt=receipt,
        program=SYSTEMATIC_SCIENTIST_POLICY,
    )
    assert score.invalid_reasons == ()
    assert score.scientific_success is True
    assert score.objective_scores["task_completion"] == 1
    assert score.objective_scores["explicit_rule_discovery"] == 1
    assert score.objective_scores["informative_trials"] == 2
    assert score.objective_scores["distinct_hypotheses_tested"] == 2
    assert score.objective_scores["objective_information_gain_bits"] == 2
    assert score.objective_scores["hypothesis_revision_rate"] == 1
    assert score.objective_scores["reproducible"] == 1
    assert set(score.evidence_objects) == {"harness_run_0", "harness_run_1"}
    assert {evidence["final_hypothesis_id"] for evidence in score.evidence_objects.values()} == {
        "substance_b"
    }


def test_real_randomized_trace_is_invalid_not_best_of_two(evaluator_bundle):
    _source, receipt, _harness, scorer, task = evaluator_bundle
    randomized = STOP_ONLY_POLICY.replace(
        b'"No experiment was run."',
        b'__import__("secrets").token_hex(16)',
    )
    score = score_program(scorer=scorer, task=task, receipt=receipt, program=randomized)
    assert score.invalid_reasons == (InvalidReason.NON_REPRODUCIBLE,)
    assert score.scientific_success is None
