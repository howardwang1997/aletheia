#!/usr/bin/env python3
"""Export one round's RawRunEnvelope and CuprateDiagnosticResult, read-only.

PAUSE-3 input (runbook 4d): with the driver STOPPED before the terminal
material is observed, export the round's exact content for offline template
authoring and the 16-field lookup assertion.  The export walks the SAME
adapter path the worker's raw_run_source service uses at COMMIT_VALIDATION
(observations/adapters.py PostgreSQLRawRunEnvelopeSourceAdapter), composed
from the DEPLOYED service's own composition config, so the bytes here are
the bytes the service will see:

    sudo -u arl2drv env PYTHONDONTWRITEBYTECODE=1 \
        /opt/aletheia/python/bin/python scripts/export_arl2_round_material.py \
        --database-url 'postgresql+psycopg://arl2drv@/<db>?host=/run/postgresql' \
        --service-config /opt/aletheia/arl2-dryrun/configs/services/raw-run-source.json \
        --quest-id <qst_> --action-sha256 <sha> --scientific-slot-id <sos_> \
        --output-dir /opt/aletheia/arl2-dryrun/staging/round-1-material

The scientific slot id comes from the round's REGISTER_EXECUTION receipt
(execution_registration.py:134-142 echoes it) or the continuation receipt.
Outputs, canonical bytes throughout: raw_run.json (the envelope),
diagnostic_result.json (the CuprateDiagnosticResult object bytes read from
the artifact store CAS by content sha), and export-state.json (the shas the
template scripts consume).  Fails loudly while the material is still
pending (raw_run:terminal_material_pending) - that means the executor has
not landed the terminal material yet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True, help="campaign database URL")
    parser.add_argument(
        "--service-config",
        required=True,
        help="the deployed raw_run_source service's composition config JSON",
    )
    parser.add_argument("--quest-id", required=True, help="campaign quest id (qst_)")
    parser.add_argument("--action-sha256", required=True, help="the round's action object sha")
    parser.add_argument(
        "--scientific-slot-id", required=True, help="the round's scientific slot id (sos_)"
    )
    parser.add_argument("--output-dir", required=True, help="directory for the exported JSONs")
    return parser


def _fail(message: str) -> None:
    raise SystemExit(f"export_arl2_round_material: {message}")


def main() -> int:
    args = _parser().parse_args()
    os.environ["ALETHEIA_DATABASE_URL"] = args.database_url

    from aletheia.db import session_factory
    from aletheia.execution.cuprate.exact_content import combined_outcome_bin_id
    from aletheia.execution.runtime_contracts import QualificationAuthorityVerifier
    from aletheia.execution.terminal_runtime import (
        QualificationTerminalReaderConfig,
        compose_qualification_raw_run_material_reader,
    )
    from aletheia.observations.adapters import (
        PostgreSQLRawRunEnvelopeSourceAdapter,
        RawRunEnvelopeSourceVerificationContext,
    )
    from aletheia.observations.scientific_bridge import (
        ScientificBridgeAuthorityPin,
        canonical_json_bytes,
    )
    from aletheia.research_controller.external_rpc import CuprateDiagnosticResult

    config = json.loads(Path(args.service_config).resolve(strict=True).read_text())
    reader = QualificationTerminalReaderConfig.model_validate(config["qualification_reader"])
    pins = {
        name: ScientificBridgeAuthorityPin.model_validate(config[f"{name}_authority_pin"])
        for name in ("execution", "validator", "admission")
    }
    source = PostgreSQLRawRunEnvelopeSourceAdapter(
        execution_material=compose_qualification_raw_run_material_reader(reader),
        sea_sessions=session_factory(),
        verification=RawRunEnvelopeSourceVerificationContext(
            qualification_authority=QualificationAuthorityVerifier(
                reader.qualification_authority_pin
            ),
            execution_authority_pin=pins["execution"],
            validator_authority_pin=pins["validator"],
            admission_authority_pin=pins["admission"],
        ),
    )
    raw_run = source.load_raw_run(
        quest_id=args.quest_id,
        action_sha256=args.action_sha256,
        scientific_slot_id=args.scientific_slot_id,
    )
    envelope_bytes = canonical_json_bytes(raw_run)
    message = raw_run.scientific_authorization.message
    artifact_key = message.scientific_observation_artifact_binding.artifact_key
    entries = [
        entry for entry in raw_run.artifact_manifest.entries
        if entry.artifact_key == artifact_key
    ]
    if len(entries) != 1:
        _fail(
            f"raw run carries {len(entries)} manifest entries for artifact key "
            f"{artifact_key!r}; exactly one was expected"
        )
    entry = entries[0]

    # The diagnostic bytes are the CAS object the receipts pin: read them by
    # content sha from the artifact store the reader is pinned to, then prove
    # the round trip model-bytes identity.
    object_path = (
        Path(reader.artifact_store_root) / "objects" / "sha256" / entry.content_sha256[:2]
        / entry.content_sha256
    )
    result_bytes = object_path.read_bytes()
    if hashlib.sha256(result_bytes).hexdigest() != entry.content_sha256:
        _fail(f"artifact store object {object_path} does not match its content sha")
    result = CuprateDiagnosticResult.model_validate_json(result_bytes)
    if canonical_json_bytes(result) != result_bytes:
        _fail("diagnostic result bytes are not the model's canonical bytes")

    output = Path(args.output_dir).resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    (output / "raw_run.json").write_bytes(envelope_bytes)
    (output / "diagnostic_result.json").write_bytes(result_bytes)
    state = {
        "schema_name": "aletheia.arl2_round_material_export",
        "schema_version": 1,
        "quest_id": args.quest_id,
        "action_sha256": args.action_sha256,
        "scientific_slot_id": args.scientific_slot_id,
        "raw_run_sha256": raw_run.raw_run_sha256,
        "artifact_key": artifact_key,
        "artifact_content_sha256": entry.content_sha256,
        "outcome_bin_id": combined_outcome_bin_id(result),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    state_bytes = canonical_json_bytes(state)
    (output / "export-state.json").write_bytes(state_bytes)
    sys.stdout.write(
        f"round material exported to {output}\n"
        f"  raw run sha      {raw_run.raw_run_sha256}\n"
        f"  artifact key     {artifact_key}\n"
        f"  artifact sha     {entry.content_sha256}\n"
        f"  outcome bin      {state['outcome_bin_id']}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
