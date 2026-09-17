#!/usr/bin/env python3
"""Stage the ARL-2 campaign request: card, CSV, splits, staged rows, launch pins.

Run as the driver uid on the box after the activation window (W1) and before
register-only (W2):

    sudo -u arl2drv env PYTHONDONTWRITEBYTECODE=1 \
        /opt/aletheia/python/bin/python scripts/author-arl2-campaign-request.py \
        --working-root /opt/aletheia/arl2-dryrun \
        --database-url 'postgresql+psycopg://arl2drv@/<db>?host=/run/postgresql' \
        --card /opt/aletheia/arl2-dryrun/staging/dataset_card_uci_superconduct_v1.json \
        --content /opt/aletheia/arl2-dryrun/staging/superconduct_unique_m.csv \
        --staged-actions /opt/aletheia/arl2-dryrun/configs/staged-actions.json

What it does, in order (register re-verifies steps 3-5 at commit time,
aletheia/arl2_runtime.py:1110-1154):

1. loads the W1 activation state; revalidates the question version and its
   exactly-3 grounding refs against the state file (never re-typed);
2. stages the card CANONICALLY (the keystone-A card file on disk is
   pretty-printed; register requires the staged bytes to BE canonical_json_bytes,
   arl2_runtime.py:522-530) and copies the CSV byte-identically, pinning both
   file shas after verify_dataset_card passes;
3. derives both rounds' sealed partitions from the card + CSV
   (sealed_groups_for_round) and the bound-batch rows from the batch rule in
   the staged-actions input (batch_max_rows over the canonically ordered
   round rows, null = the whole partition);
4. pins per round: split bindings (sealed/spent/unspent/batch set hashes),
   staged rows, and a staged-rows artifact written canonically whose bytes
   sha becomes staged_artifact_sha256;
5. reads the live launch pins from the store audit (stream version 3, the
   QUESTION_ADMITTED tail sha, the reduced-state snapshot sha) - hand-typed
   pins are impossible because the pins are checked at launch, not register
   (launch.py:31-60);
6. assembles ARL2QuestionCampaignRequestV1, runs verify_dataset_card +
   verify_pre_registered_round_splits + the replay-time round-split-binding
   projections as a pre-flight, and writes the request plus
   configs/arl2-request-state.json for author-arl2-deployments.py.

The staged ACTION shas and per-round proposal request shas come from the
staged-actions input verbatim.  At W2 they are placeholders: the real action
object shas cover a wall-clock proposed_at stamped during apply
(runbook Q9, design contradiction #6), so this script takes no position on
where they come from - it only refuses to invent them.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_SHA256_PATTERN = "0123456789abcdef"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--working-root",
        required=True,
        help="absolute path to the window working root (staging/, configs/)",
    )
    parser.add_argument(
        "--database-url",
        required=True,
        help="campaign database URL (peer-auth unix socket form)",
    )
    parser.add_argument(
        "--card",
        required=True,
        help="keystone-A dataset card JSON (any on-disk formatting)",
    )
    parser.add_argument(
        "--content",
        required=True,
        help="keystone-A dataset CSV (byte-identical to the card's content)",
    )
    parser.add_argument(
        "--staged-actions",
        required=True,
        help=(
            "per-round input JSON: {\"1\": {\"action_sha256\": ..., "
            "\"proposal_request_sha256\": ..., \"batch_max_rows\": 12000}, "
            "\"2\": {..., \"batch_max_rows\": null}}"
        ),
    )
    parser.add_argument(
        "--activation-state",
        required=False,
        default=None,
        help="path to the W1 activation state (default: <working-root>/configs/arl2-activation-state.json)",
    )
    return parser


def _fail(message: str) -> None:
    raise SystemExit(f"author-arl2-campaign-request: {message}")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256_PATTERN for character in value)
    )


def _canonical_bytes(payload) -> bytes:
    from aletheia.research_kernel.schemas import canonical_json_bytes

    return canonical_json_bytes(payload)


def _write_canonical(path: Path, payload) -> tuple[str, str]:
    data = _canonical_bytes(payload)
    return _write_bytes(path, data)


def _write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> tuple[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(data)
        staged = Path(handle.name)
    os.replace(staged, path)
    if mode is not None:
        # mkstemp files land 0600 owner-writable; callers that stage shared,
        # read-only inputs pin the final mode explicitly after the replace
        os.chmod(path, mode)
    return str(path), hashlib.sha256(data).hexdigest()


def _set_sha(group_ids) -> str:
    # Mirrors campaign_replay._set_sha (canonical sha over the id list); the
    # production callers always pass a sorted-unique tuple, so sort here too.
    from aletheia.research_kernel.schemas import canonical_sha256

    return canonical_sha256(list(sorted(set(group_ids))))


def _load_json(path: Path) -> dict:
    loaded = json.loads(path.read_text())
    if not isinstance(loaded, dict):
        _fail(f"{path} does not hold a JSON object")
    return loaded


def _load_staged_actions(path: Path) -> dict:
    raw = _load_json(path)
    if set(raw) != {"1", "2"}:
        _fail("staged-actions input must carry exactly the rounds \"1\" and \"2\"")
    rounds = {}
    for index in ("1", "2"):
        entry = raw[index]
        if not isinstance(entry, dict):
            _fail(f"staged-actions round {index} is not an object")
        for field in ("action_sha256", "proposal_request_sha256"):
            if not _is_sha256(entry.get(field)):
                _fail(f"staged-actions round {index} is missing a valid {field}")
        max_rows = entry.get("batch_max_rows", None)
        if max_rows is not None and (not isinstance(max_rows, int) or max_rows < 1):
            _fail(f"staged-actions round {index} batch_max_rows must be a positive int or null")
        rounds[int(index)] = {
            "action_sha256": entry["action_sha256"],
            "proposal_request_sha256": entry["proposal_request_sha256"],
            "batch_max_rows": max_rows,
        }
    if rounds[1]["action_sha256"] == rounds[2]["action_sha256"]:
        _fail("staged-actions rounds must name two distinct actions")
    return rounds


def _parse_round_pairs(csv_bytes: bytes, card) -> tuple[tuple[str, float], ...]:
    """(composition, target) pairs under the card's manifest, mirroring
    enumerate_formula_groups' finite-float filter (data_registration.py:319-353)."""

    manifest = card.column_manifest
    reader = csv.reader(io.StringIO(csv_bytes.decode("utf-8")))
    try:
        header = [cell.strip() for cell in next(reader)]
    except StopIteration:
        _fail("dataset content is empty")
    if len(set(header)) != len(header) or set(header) != set(manifest.columns):
        _fail("dataset content header differs from the card's column manifest")
    composition_at = header.index(manifest.composition_column)
    target_at = header.index(manifest.target_column)
    pairs = set()
    for row in reader:
        if len(row) <= max(composition_at, target_at):
            continue
        composition = row[composition_at].strip()
        if not composition:
            continue
        try:
            target = float(row[target_at])
        except ValueError:
            continue
        if math.isfinite(target):
            pairs.add((composition, target))
    if not pairs:
        _fail("dataset content carries no parsable target rows")
    return tuple(sorted(pairs))


def main() -> int:
    args = _parser().parse_args()
    os.environ["ALETHEIA_DATABASE_URL"] = args.database_url

    from aletheia.arl2_runtime import ARL2QuestionCampaignRequestV1
    from aletheia.protocols.data_registration import (
        RegisteredDatasetV1,
        verify_dataset_card,
    )
    from aletheia.research_controller.campaign_replay import (
        StagedRowV1,
        StagedRoundRowsV1,
        sealed_groups_for_round,
        verify_pre_registered_round_splits,
    )
    from aletheia.research_controller.contracts import ResearchControllerLaunchRequest
    from aletheia.research_controller.protocol_compilation_step import (
        RoundSplitBindingPolicyV1,
        RoundSplitTemplateBindingV1,
    )
    from aletheia.research_controller.world_model_revision import (
        verify_round_split_binding,
    )
    from aletheia.research_kernel.policy import (
        ResearchAuthorizationPolicyV1,
        ResearchAuthorizationTrustRootV1,
    )
    from aletheia.research_kernel.schemas import EventType, ResearchQuestionVersion
    from aletheia.research_store.cas import FilesystemResearchArchive
    from aletheia.research_store.store import ResearchKernelStore

    working_root = Path(args.working_root).resolve(strict=False)
    if not working_root.is_dir():
        _fail(f"working root {working_root} does not exist")
    state_path = working_root / "configs" / "arl2-request-state.json"
    if state_path.exists():
        _fail(
            "request state already exists; move it aside deliberately before "
            "re-authoring (a re-authored request is a new campaign)"
        )
    staging = working_root / "staging"

    state = _load_json(
        Path(args.activation_state).resolve(strict=True)
        if args.activation_state
        else working_root / "configs" / "arl2-activation-state.json"
    )
    authority = _load_json(Path(state["authority_manifest_path"]))
    quest_id = state["quest_id"]

    question = ResearchQuestionVersion.model_validate(state["question_version"])
    if question.object_sha256 != state["question_object_sha256"]:
        _fail("activation-state question version does not match its recorded object sha")
    if question.quest_id != quest_id:
        _fail("activation-state question version belongs to another quest")
    grounding = tuple(sorted(set(state["grounding_object_sha256s"])))
    if tuple(sorted(item.object_sha256 for item in question.evidence_refs)) != grounding:
        _fail("activation-state grounding shas differ from the question's evidence refs")

    # 2. card + CSV: verify first, then stage canonically and byte-identically.
    card_path = Path(args.card).resolve(strict=True)
    content_path = Path(args.content).resolve(strict=True)
    card = RegisteredDatasetV1.model_validate(json.loads(card_path.read_text()))
    csv_bytes = content_path.read_bytes()
    if hashlib.sha256(csv_bytes).hexdigest() != card.content_sha256:
        _fail("dataset content bytes differ from the card's content sha")
    verify_dataset_card(card, csv_bytes)
    card_file, card_sha = _write_canonical(staging / "arl2-dataset-card.json", card)
    if card_sha != card.dataset_card_sha256:
        _fail("canonical card staging does not reproduce the card's dataset_card_sha256")
    # The cuprate diagnostic service reads the CSV as a distinct uid and its
    # staged_dataset_custody gate rejects ANY write bit on the file, so the
    # staged copy is world-readable and read-only (public UCI content; the
    # register-side re-read still pins the content sha).
    content_file, content_sha = _write_bytes(
        staging / "arl2-dataset-content.csv", csv_bytes, mode=0o444
    )
    if content_sha != card.content_sha256:
        _fail("canonical content staging changed the dataset bytes")

    # 3. partitions and bound-batch rows.
    sealed = {
        index: sealed_groups_for_round(card, csv_bytes, round_index=index)
        for index in (1, 2)
    }
    actions = _load_staged_actions(Path(args.staged_actions).resolve(strict=True))
    all_pairs = _parse_round_pairs(csv_bytes, card)
    round_pairs = {
        index: tuple(pair for pair in all_pairs if pair[0] in set(sealed[index]))
        for index in (1, 2)
    }
    batch_rows = {
        index: (
            round_pairs[index]
            if actions[index]["batch_max_rows"] is None
            else round_pairs[index][: actions[index]["batch_max_rows"]]
        )
        for index in (1, 2)
    }
    batch_groups = {
        index: sorted({composition for composition, _ in batch_rows[index]})
        for index in (1, 2)
    }
    for index in (1, 2):
        if not batch_rows[index]:
            _fail(f"round {index} bound batch is empty")
        if len(batch_rows[index]) > len(round_pairs[index]):
            _fail(f"round {index} batch rule escaped the round's partition")

    # 4. staged-rows artifacts, staged rounds, split bindings.
    staged = {}
    artifacts = {}
    for index in (1, 2):
        artifact_payload = {
            "schema_name": "aletheia.arl2_staged_rows_artifact",
            "schema_version": 1,
            "round_index": index,
            "rows": [
                {"composition": composition, "target": target}
                for composition, target in batch_rows[index]
            ],
        }
        artifacts[index] = _write_canonical(
            staging / f"arl2-staged-round-{index}-rows.json", artifact_payload
        )
        staged[index] = StagedRoundRowsV1(
            round_index=index,
            action_sha256=actions[index]["action_sha256"],
            request_sha256=actions[index]["proposal_request_sha256"],
            staged_artifact_sha256=artifacts[index][1],
            rows=tuple(
                StagedRowV1(composition=composition, target=target)
                for composition, target in batch_rows[index]
            ),
        )
    # Raw-ledger convention (campaign_replay.py D5/D6): round 1 pins the empty
    # ledger; round 2 pins round 1's admitted batch.  Unspent is always the
    # round's OWN partition.
    ledger_groups = {1: (), 2: tuple(batch_groups[1])}
    bindings = tuple(
        RoundSplitBindingPolicyV1(
            dataset_content_sha256=card.content_sha256,
            split_policy_sha256=card.split_policy.policy_sha256,
            round_index=index,
            sealed_group_ids_sha256=_set_sha(sealed[index]),
            template_bindings=(
                RoundSplitTemplateBindingV1(
                    action_sha256=actions[index]["action_sha256"],
                    bound_batch_group_ids_sha256=_set_sha(batch_groups[index]),
                    spent_group_ids_sha256=_set_sha(ledger_groups[index]),
                    unspent_group_ids_sha256=_set_sha(sealed[index]),
                ),
            ),
        )
        for index in (1, 2)
    )

    # 5. live launch pins from the store audit (never hand-typed).
    trust_root = ResearchAuthorizationTrustRootV1.model_validate(authority["trust_root"])
    policy = ResearchAuthorizationPolicyV1.model_validate(authority["policy"])
    # This script runs as the driver uid, the identity that OWNS the writer
    # CAS root, so a read_only compose is impossible here: cas.py gives the
    # owner the owner permission class, which on a 0700 writer root always
    # carries the write bit. Open the writer-handle archive exactly as the
    # runtime driver does (arl2_runtime._compose_archive) and audit through
    # it read-only-in-intent; nothing in this script writes through it.
    archive = FilesystemResearchArchive(
        Path(authority["cas_root"]),
        max_object_bytes=64 * 1024 * 1024,
        read_only=False,
        directory_mode=0o700,
        object_mode=0o400,
    )
    store = ResearchKernelStore(trust_root=trust_root, archive=archive, genesis_policy=policy)
    audit = store.audit(quest_id)
    if len(audit.events) != 3 or audit.events[-1].event_type is not EventType.QUESTION_ADMITTED:
        _fail("store audit does not end at the three-event activation (charter/problem/question)")
    tail_sha = audit.events[-1].event_sha256
    if tail_sha != state["question_event_sha256"]:
        _fail("store audit tail differs from the activation state's question event sha")
    launch = ResearchControllerLaunchRequest(
        program_id=(
            "prg_"
            + hashlib.sha256(f"{quest_id}:arl2-dryrun-program".encode()).hexdigest()[:32]
        ),
        quest_id=quest_id,
        idempotency_key=f"arl2dry:{quest_id}:campaign-launch",
        expected_stream_version=3,
        expected_tail_event_sha256=tail_sha,
        expected_snapshot_sha256=audit.state.snapshot_sha256,
    )

    # 6. assemble, pre-flight, write.
    request = ARL2QuestionCampaignRequestV1(
        quest_id=quest_id,
        launch_request=launch,
        question_version=question,
        grounding_object_sha256s=grounding,
        dataset_card_path=card_file,
        dataset_card_file_sha256=card_sha,
        dataset_content_path=content_file,
        dataset_content_file_sha256=content_sha,
        round_split_bindings=bindings,
        staged_rounds=(staged[1], staged[2]),
    )
    verify_dataset_card(card, csv_bytes)
    verify_pre_registered_round_splits(
        card=card, csv_bytes=csv_bytes, round_split_bindings=request.round_split_bindings
    )
    for index in (1, 2):
        sealed_set = set(sealed[index])
        ledger_set = set(ledger_groups[index])
        verify_round_split_binding(
            sealed_group_ids=sealed[index],
            spent_group_ids=tuple(g for g in ledger_groups[index] if g in sealed_set),
            unspent_group_ids=tuple(g for g in sealed[index] if g not in ledger_set),
            bound_group_ids=tuple(batch_groups[index]),
        )

    request_file, request_sha = _write_canonical(
        staging / "arl2-campaign-request.json", request
    )
    _write_canonical(
        state_path,
        {
            "schema_name": "aletheia.arl2_request_state",
            "schema_version": 1,
            "quest_id": quest_id,
            "request_path": request_file,
            "request_sha256": request_sha,
            "request_id": request.request_id,
            "dataset_card": {"path": card_file, "file_sha256": card_sha},
            "dataset_content": {"path": content_file, "file_sha256": content_sha},
            "launch_pins": {
                "expected_stream_version": 3,
                "expected_tail_event_sha256": tail_sha,
                "expected_snapshot_sha256": audit.state.snapshot_sha256,
            },
            "staged_rounds": {
                str(index): {
                    "action_sha256": actions[index]["action_sha256"],
                    "proposal_request_sha256": actions[index]["proposal_request_sha256"],
                    "staged_artifact_path": artifacts[index][0],
                    "staged_artifact_sha256": artifacts[index][1],
                    "batch_group_count": len(batch_groups[index]),
                    "batch_row_count": len(batch_rows[index]),
                    "sealed_group_count": len(sealed[index]),
                }
                for index in (1, 2)
            },
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        },
    )
    sys.stdout.write(
        f"campaign request staged for quest {quest_id}\n"
        f"  request sha   {request_sha}\n"
        f"  launch tail   {tail_sha}\n"
        f"  launch snap   {audit.state.snapshot_sha256}\n"
        f"  round 1 batch {len(batch_rows[1])} rows / {len(batch_groups[1])} groups "
        f"of {len(sealed[1])} sealed\n"
        f"  round 2 batch {len(batch_rows[2])} rows / {len(batch_groups[2])} groups "
        f"of {len(sealed[2])} sealed\n"
        f"  state         {state_path}\n"
        f"NOTE staged action shas come from the input verbatim; at W2 they are "
        f"placeholders (runbook Q9)\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
