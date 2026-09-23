#!/usr/bin/env python3
"""Dispatch one commissioned external qualification execution (bridge kit).

The bridge-dispatch tool contradiction #20 named: the allocator carries the
eight public external lifecycle methods, but the box had no operator surface
that drives them.  This script is that surface.  It composes the deployment's
PostgreSQL allocator from the frozen deployment state (registry-backed
authority resolution, the pinned artifact store, the node/bridge/terminal
authorities), admits the commissioned qualification pair, runs the workload
under bridge custody, and settles the attempt through the terminal outbox.
It finishes by replaying the settled history through the read-only readers
(terminal source, external run lineage, external raw-run material, outbox)
and prints one evidence document -- the take-5 W1 executor-dispatch seam.

Two verbs:

  dispatch (default)
      admit (or attach to) one external attempt and drive
      reserved -> starting -> running -> verifying -> succeeded.  The
      workload runs as a subprocess under the invoking identity; its stdout,
      stderr, and output files become the termination evidence and the
      terminal artifact manifest.  Every signed ladder contract is written
      under <working-root>/dispatch/<attempt_id>/ before the corresponding
      allocator call, so a crashed run re-executed for the same attempt
      replays the stored bytes instead of signing divergent history (the
      allocator's replay is exact-sha; a re-derived contract would be a
      second generation and is refused).

      Crash windows, honestly: a resume before launch-accept re-runs the
      workload (kill any orphan from the crashed run first); a resume after
      the workload completed but before its outcome was persisted replays
      from the stored outcome; a crash between the two loses the exit facts
      with the dead process, and the driver refuses to invent them -- the
      runbook answer there is --adjudicate after the artifact deadline.

  --adjudicate
      one deadline sweep for the deployment's bridge manifest: activate the
      pre-signed terminal expiration of every attempt whose artifact
      submission deadline passed without a terminal submission.  Nothing in
      the deployed runtime calls the adjudicator today; this verb is the
      operational sweeper the runbook schedules.

Lease-token custody: the one-time admission token never leaves the admitting
process, so the driver stores it once at 0400 under the attempt's 0700
dispatch directory and re-reads it (sha-checked) when a re-run attaches to
its own earlier admission.  A dispatch attaching to a foreign admission must
be handed the token explicitly via --lease-token-file (0400, raw token, one
line); without either custody source the driver fails closed.

Inputs, all commissioned material -- nothing is hand-typed:

  --deployment-state  configs/arl2-deployment-state.json
                      (author-arl2-deployments.py; the driver reads the
                      qualification reader config, the external-bridge pin,
                      the runtime-control pin, and the key files under
                      <working-root>/keys/qualification/)
  --database-url      the campaign database; its sha must equal the state's
  --sea-template      the frozen SEA template (PAUSE-2), or --bundle +
                      --grant with the two models as canonical JSON
  --workload-command  argv of the executor command; it receives
                      ALETHEIA_WORKLOAD_OUTPUT (the directory this script
                      passes as --workload-output, which must already exist)
                      plus ALETHEIA_EXECUTION_ID / ALETHEIA_ATTEMPT_ID, and
                      writes its artifacts there before exiting
  --acknowledge       DISPATCH_ARL2_EXTERNAL_EXECUTION

Timing honesty: the ladder runs on real database time (no clock control).
Deterministic stage contracts are anchored to the attempt's reserved_at, and
observed contracts (launch receipt, termination receipt, submission) record
the wall/monotonic facts of this run, persisted before the allocator call
they accompany.  The authorization window between authorize and
launch-accept is thirty seconds by default; a resumed run must reach
accept_external_runtime_launch promptly after the authorize replay.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ACKNOWLEDGEMENT = "DISPATCH_ARL2_EXTERNAL_EXECUTION"
STATE_SCHEMA = "aletheia.arl2_deployment_state"
EVIDENCE_SCHEMA = "aletheia.arl2_external_dispatch_evidence"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-state", required=True)
    parser.add_argument("--database-url", required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--sea-template", help="frozen SEA template carrying the bundle and grant")
    source.add_argument("--bundle", help="canonical EngineeringQualificationBundle JSON")
    parser.add_argument("--grant", help="canonical EngineeringQualificationGrant JSON")
    parser.add_argument(
        "--lease-token-file",
        help="0400 custody file holding the one-time admission token (attach dispatch)",
    )
    parser.add_argument(
        "--runtime-control-key",
        help="runtime-control private key (default <working-root>/keys/qualification/runtime_control.key)",
    )
    parser.add_argument(
        "--bridge-key",
        help="external-bridge private key (default <working-root>/keys/qualification/external-bridge.key)",
    )
    parser.add_argument(
        "--workload-command",
        nargs=argparse.REMAINDER,
        help="executor argv (dispatch verb); pass LAST -- it consumes the rest of "
        "the command line, flags included",
    )
    parser.add_argument(
        "--workload-output",
        help="existing directory the workload writes its artifacts into (dispatch verb)",
    )
    parser.add_argument(
        "--adjudicate",
        action="store_true",
        help="run one terminal-deadline sweep instead of a dispatch",
    )
    parser.add_argument("--evidence", help="write the evidence JSON here (default stdout only)")
    parser.add_argument("--acknowledge", required=True)
    return parser


def _fail(message: str) -> None:
    raise SystemExit(f"dispatch-arl2-external-execution: {message}")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(encoded)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _read_bytes(path: Path) -> bytes:
    return Path(path).resolve(strict=True).read_bytes()


def _write_once(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    if path.exists():
        _fail(f"refusing to overwrite existing dispatch record {path}")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(descriptor, payload)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def _public_key_hex(private_key: bytes) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return (
        Ed25519PrivateKey.from_private_bytes(bytes(private_key))
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )


def _load_key(path: Path, *, pin_public_hex: str, label: str) -> bytes:
    material = _read_bytes(path)
    if len(material) != 32:
        _fail(f"{label} key file {path} must hold exactly 32 raw bytes")
    if _public_key_hex(material) != pin_public_hex:
        _fail(f"{label} key file {path} differs from its deployment pin")
    return material


# --------------------------------------------------------------------------
# Deployment-state composition (the deployed box; unit tests patch this seam)
# --------------------------------------------------------------------------


def _compose_allocator(state: dict, state_path: Path, args):
    """Build the deployment allocator: reader config, registries, keys, bridge."""

    from aletheia.execution.allocator import (
        LocalPricingAuthorityPin,
        PostgreSQLExecutionAllocator,
        PostgreSQLExecutionReceiptArchive,
    )
    from aletheia.execution.artifact_store import LocalArtifactStore
    from aletheia.execution.authority_registry import (
        CompositeExecutionAuthorityResolver,
        ExactExecutionCostQuoteRegistry,
        SourceBudgetProjectionRegistry,
    )
    from aletheia.execution.input_resolver import LocalVerifiedInputArtifactResolver
    from aletheia.execution.runtime_contracts import (
        ExternalBridgeAuthority,
        QualificationAuthorityPin,
        QualificationAuthorityVerifier,
        WorkerNodeAuthorityVerifier,
    )
    from aletheia.execution.runtime_control_issuance import (
        PinnedRuntimeControlIssuanceAuthority,
    )
    from aletheia.execution.runtime_v2_contracts import (
        NodeEnrollmentAuthorityVerifier,
        PinnedRuntimeControlVerificationAuthority,
    )
    from aletheia.execution.terminal_runtime import QualificationTerminalReaderConfig
    from aletheia.execution.terminal_verification import (
        TerminalVerificationAuthorityVerifier,
    )

    reader = QualificationTerminalReaderConfig.model_validate(
        state["qualification"]["reader"]
    )
    bridge_pin_payload = state["qualification"].get("external_bridge_pin")
    if not isinstance(bridge_pin_payload, dict):
        _fail(
            "deployment state carries no external-bridge pin; the window was "
            "commissioned without an external resource class"
        )
    bridge_pin = QualificationAuthorityPin.model_validate(bridge_pin_payload)
    if len(reader.node_authorities) != 1:
        _fail("the bridge kit dispatches against exactly one commissioned node manifest")
    node_authority_config = reader.node_authorities[0]

    working_root = state_path.parent.parent
    runtime_control_key = _load_key(
        Path(args.runtime_control_key)
        if args.runtime_control_key is not None
        else working_root / "keys" / "qualification" / "runtime_control.key",
        pin_public_hex=reader.runtime_control_authority_pin.public_key_ed25519_hex,
        label="runtime-control",
    )
    bridge_key = _load_key(
        Path(args.bridge_key)
        if args.bridge_key is not None
        else working_root / "keys" / "qualification" / "external-bridge.key",
        pin_public_hex=bridge_pin.public_key_ed25519_hex,
        label="external-bridge",
    )

    terminal_verifier = TerminalVerificationAuthorityVerifier(
        reader.terminal_verification_authority_pin
    )
    receipt_archive = PostgreSQLExecutionReceiptArchive(
        terminal_verification_authority=terminal_verifier
    )
    artifact_store = LocalArtifactStore(
        Path(reader.artifact_store_root),
        verifier_principal_id=reader.artifact_verifier_principal_id,
        object_store_id=reader.artifact_object_store_id,
        max_object_bytes=reader.artifact_max_object_bytes,
        read_only=True,
    )
    artifact_resolver = LocalVerifiedInputArtifactResolver(
        artifact_store=artifact_store,
        terminal_receipt_archive=receipt_archive,
        resolver_principal_id=reader.input_resolver_principal_id,
    )
    execution_authority_resolver = CompositeExecutionAuthorityResolver(
        quote_registry=ExactExecutionCostQuoteRegistry(
            Path(reader.authority_registry_root),
            filesystem_pin=reader.authority_registry_filesystem_pin,
            pricing_authority_pin=reader.pricing_authority_pin,
        ),
        budget_registry=SourceBudgetProjectionRegistry(
            Path(reader.authority_registry_root),
            filesystem_pin=reader.authority_registry_filesystem_pin,
            source_budget_authority_pin=reader.source_budget_authority_pin,
        ),
        execution_receipt_resolver=receipt_archive,
    )
    node_authority = WorkerNodeAuthorityVerifier(
        manifest=node_authority_config.manifest,
        enrollment=node_authority_config.enrollment,
        enrollment_authority=NodeEnrollmentAuthorityVerifier(
            node_authority_config.enrollment_authority_pin
        ),
        expected_manifest_sha256=node_authority_config.manifest.manifest_sha256,
        observed_at=reader.prepared_at,
    )
    bridge_authority = ExternalBridgeAuthority(
        manifest=node_authority_config.manifest,
        bridge_authority_pin=bridge_pin,
    )
    allocator = PostgreSQLExecutionAllocator(
        authority=QualificationAuthorityVerifier(reader.qualification_authority_pin),
        artifact_resolver=artifact_resolver,
        execution_authority_resolver=execution_authority_resolver,
        pricing_authority=LocalPricingAuthorityPin(
            quote_principal_ids=frozenset({reader.pricing_authority_pin.principal_id}),
            rate_card_sha256s=frozenset(reader.allowed_rate_card_sha256s),
            pricing_policy_sha256s=frozenset({reader.pricing_authority_pin.policy_sha256}),
            currency_codes=frozenset(reader.allowed_currency_codes),
        ),
        node_authorities=(node_authority,),
        node_assignment_transport_pins=(node_authority_config.assignment_transport_pin,),
        external_bridge_authorities=(bridge_authority,),
        terminal_verification_authority=terminal_verifier,
        allocator_principal_id=reader.allocator_principal_id,
        runtime_control_issuer=PinnedRuntimeControlIssuanceAuthority(
            pin=reader.runtime_control_authority_pin,
            private_key=runtime_control_key,
        ),
        runtime_control_authority=PinnedRuntimeControlVerificationAuthority(
            reader.runtime_control_authority_pin
        ),
    )
    return allocator, reader, bridge_authority, bridge_key


# --------------------------------------------------------------------------
# Dispatch verb
# --------------------------------------------------------------------------


def _load_pair(args: argparse.Namespace):
    from aletheia.execution.runtime_contracts import (
        EngineeringQualificationBundle,
        EngineeringQualificationGrant,
    )

    if args.sea_template is not None:
        from aletheia.research_controller.execution_authorization_service import (
            FrozenScientificExecutionAuthorizationTemplate,
        )

        template = FrozenScientificExecutionAuthorizationTemplate.model_validate_json(
            _read_bytes(Path(args.sea_template))
        )
        return template.qualification_bundle, template.qualification_grant
    if args.bundle is None or args.grant is None:
        _fail("dispatch needs --sea-template or both --bundle and --grant")
    bundle = EngineeringQualificationBundle.model_validate_json(
        _read_bytes(Path(args.bundle))
    )
    grant = EngineeringQualificationGrant.model_validate_json(
        _read_bytes(Path(args.grant))
    )
    return bundle, grant


class _Ladder:
    """Load-or-sign persistence for every ladder contract of one attempt."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, name: str) -> Path:
        return self.root / f"{name}.json"

    def load_or_build(self, name: str, model_class, build):
        path = self.path(name)
        if path.exists():
            return model_class.model_validate_json(_read_bytes(path))
        instance = build()
        _write_once(path, instance.model_dump_json(indent=2).encode("utf-8"))
        return instance


def _absent(name: str):
    def build():
        _fail(f"dispatch record {name} is missing for the resumed attempt")

    return build


def _recover_lease_token(ladder: _Ladder, snapshot, args) -> str:
    """Re-attach the one-time admission token from a 0400 custody source."""

    custody = (
        Path(args.lease_token_file).resolve(strict=True)
        if args.lease_token_file is not None
        else ladder.root / "lease-token"
    )
    if not custody.exists():
        _fail(
            f"attempt {snapshot.attempt_id} was reserved by another process; the "
            "one-time lease token cannot be re-derived from its hash -- hand it "
            "over via --lease-token-file (0400) or dispatch a fresh commissioned pair"
        )
    if stat.S_IMODE(custody.stat().st_mode) != 0o400:
        _fail(f"lease token custody file {custody} must hold mode 0400")
    token = _read_bytes(custody).decode("utf-8").strip()
    if _sha256_bytes(token.encode("utf-8")) != snapshot.lease_token_sha256:
        _fail("the lease token custody file does not hash to the attempt's pinned token")
    return token


def _dispatch(allocator, reader, bridge_authority, bridge_key, args) -> int:
    from aletheia.execution.external_bridge_contracts import (
        ExternalExecutorIdentity,
        ExternalLaunchEvidence,
        ExternalRuntimePreparation,
        ExternalTerminationEvidence,
        issue_external_qualification_terminal_submission,
        issue_external_runtime_launch_receipt,
        issue_external_runtime_termination_receipt,
        recompute_external_disposition,
    )
    from aletheia.execution.runtime_v2_contracts import (
        RuntimeLaunchAuthorizationRequest,
    )
    from aletheia.execution.schemas import (
        ArtifactManifest,
        ArtifactManifestEntry,
        ArtifactVerifiedReceipt,
    )

    if not args.workload_command or not args.workload_output:
        _fail("dispatch needs --workload-command and --workload-output")
    workload_output = Path(args.workload_output).resolve(strict=True)
    if not workload_output.is_dir():
        _fail(f"--workload-output {workload_output} is not a directory")

    bundle, grant = _load_pair(args)
    claim = allocator.admit_and_reserve(bundle=bundle, grant=grant)
    snapshot = claim.snapshot
    if snapshot.external_resource_class_id is None:
        _fail(
            f"attempt {snapshot.attempt_id} is not an external bridge attempt "
            "(the commissioned bundle resolved to node custody)"
        )
    ladder = _Ladder(
        Path(args.deployment_state).resolve().parent.parent
        / "dispatch"
        / snapshot.attempt_id
    )
    if claim.lease_token is not None:
        lease_token = claim.lease_token
        _write_once(
            ladder.root / "lease-token",
            (lease_token + "\n").encode("utf-8"),
            mode=0o400,
        )
    else:
        lease_token = _recover_lease_token(ladder, snapshot, args)

    manifest = bridge_authority.manifest
    bridge_pin = bridge_authority.bridge_authority_pin
    intent = bundle.intent
    reserved_at = snapshot.reserved_at

    # ---- deterministic preparation and request (replay-stable) ----------
    executable = args.workload_command[0]
    executable_path = Path(executable)
    workload_executable_sha256 = (
        _sha256_bytes(_read_bytes(executable_path))
        if executable_path.exists()
        else _sha256_bytes(executable.encode("utf-8"))
    )
    launch_spec = {
        "argv": list(args.workload_command),
        "workload_output": workload_output.name,
        "input_bindings": [
            binding.artifact_verified_receipt_sha256
            for binding in sorted(
                intent.input_artifact_bindings, key=lambda item: item.input_port_id
            )
        ],
    }
    runtime_id = f"bridge-runtime:{_canonical_digest(launch_spec)[:32]}"
    preparation = ladder.load_or_build(
        "runtime-preparation",
        ExternalRuntimePreparation,
        lambda: ExternalRuntimePreparation(
            bridge_manifest_sha256=manifest.manifest_sha256,
            execution_id=snapshot.execution_id,
            infrastructure_attempt_id=snapshot.attempt_id,
            intent_sha256=snapshot.intent_sha256,
            runtime_id=runtime_id,
            runtime_engine=manifest.container_runtime,
            launch_spec_sha256=_canonical_digest(launch_spec),
            workload_executable_sha256=workload_executable_sha256,
            workload_argv=tuple(args.workload_command),
            runtime_request_sha256=_canonical_digest(
                {
                    "execution_id": snapshot.execution_id,
                    "attempt_id": snapshot.attempt_id,
                    "fencing_epoch": snapshot.fencing_epoch,
                    "lease_token_sha256": snapshot.lease_token_sha256,
                }
            ),
            enforced_placement_sha256=_canonical_digest(
                {
                    "external_resource_class_id": snapshot.external_resource_class_id,
                    "node_id": manifest.node_id,
                }
            ),
            input_materialization_receipt_sha256=_canonical_digest(
                launch_spec["input_bindings"]
            ),
            fencing_epoch=snapshot.fencing_epoch,
            lease_token_sha256=snapshot.lease_token_sha256,
            prepared_dispatch_locator_sha256=_sha256_bytes(str(ladder.root).encode("utf-8")),
            prepared_at=reserved_at,
            prepared_monotonic_ns=1,
        ),
    )
    request = ladder.load_or_build(
        "launch-authorization-request",
        RuntimeLaunchAuthorizationRequest,
        lambda: RuntimeLaunchAuthorizationRequest(
            request_nonce_sha256=_canonical_digest(
                {"preparation": preparation.preparation_sha256, "argv": launch_spec["argv"]}
            ),
            runtime_preparation_sha256=preparation.preparation_sha256,
            infrastructure_attempt_id=snapshot.attempt_id,
            fencing_epoch=snapshot.fencing_epoch,
            lease_token_sha256=snapshot.lease_token_sha256,
            pre_runtime_absence_epoch=0,
            pre_runtime_absence_receipt_sha256=None,
            requested_at=reserved_at,
            requested_monotonic_ns=2,
        ),
    )
    _clock_note("authorizing", snapshot.attempt_id)
    start = allocator.authorize_external_runtime_start(
        attempt_id=snapshot.attempt_id,
        lease_token=lease_token,
        fencing_epoch=snapshot.fencing_epoch,
        runtime_preparation=preparation,
        launch_authorization_request=request,
    )

    # ---- workload + launch receipt (observed; persisted before accept) ---
    outcome_path = ladder.root / "workload-outcome.json"
    launch_receipt_path = ladder.path("launch-receipt")
    if outcome_path.exists():
        # resumed after the workload completed: reload the signed launch facts
        process = None
    elif launch_receipt_path.exists():
        _fail(
            f"attempt {snapshot.attempt_id}: the crashed run lost the workload's "
            "exit facts with its process -- adjudicate after the artifact deadline"
        )
    else:
        started_at = _utc_now()
        started_monotonic_ns = time.monotonic_ns()
        process = subprocess.Popen(
            list(args.workload_command),
            cwd=str(ladder.root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **{
                    key: value
                    for key, value in os.environ.items()
                    if key in ("PATH", "LANG", "LC_ALL", "HOME", "TMPDIR")
                },
                "ALETHEIA_EXECUTION_ID": snapshot.execution_id,
                "ALETHEIA_ATTEMPT_ID": snapshot.attempt_id,
                "ALETHEIA_WORKLOAD_OUTPUT": str(workload_output),
            },
        )
        identity = ladder.load_or_build(
            "executor-identity",
            ExternalExecutorIdentity,
            lambda: ExternalExecutorIdentity(
                execution_id=snapshot.execution_id,
                infrastructure_attempt_id=snapshot.attempt_id,
                runtime_id=runtime_id,
                executor_ref=f"bridge://{manifest.node_id}/{Path(executable).name}",
                executor_implementation_sha256=workload_executable_sha256,
                invocation_payload_sha256=_canonical_digest(launch_spec),
                started_at=started_at,
                started_monotonic_ns=started_monotonic_ns,
            ),
        )
        evidence = ladder.load_or_build(
            "launch-evidence",
            ExternalLaunchEvidence,
            lambda: ExternalLaunchEvidence(
                preparation_sha256=preparation.preparation_sha256,
                external_launch_authorization_sha256=(
                    start.launch_authorization.authorization_sha256
                ),
                executor_identity=identity,
                executor_identity_sha256=identity.executor_identity_sha256,
                executor_start_monotonic_lower_bound_ns=started_monotonic_ns,
                executor_start_monotonic_upper_bound_exclusive_ns=(
                    started_monotonic_ns + 1
                ),
                enforced_placement_sha256=preparation.enforced_placement_sha256,
                input_materialization_receipt_sha256=(
                    preparation.input_materialization_receipt_sha256
                ),
                enforced_fencing_epoch=preparation.fencing_epoch,
                enforced_lease_token_sha256=preparation.lease_token_sha256,
                launch_evidence_journal_sha256=_canonical_digest(
                    {
                        "argv": launch_spec["argv"],
                        "pid": process.pid,
                        "started_at": identity.started_at.isoformat(),
                    }
                ),
                observed_at=_utc_now(),
                observed_monotonic_ns=time.monotonic_ns(),
            ),
        )
        receipt = ladder.load_or_build(
            "launch-receipt",
            _launch_receipt_model(),
            lambda: issue_external_runtime_launch_receipt(
                bridge_pin=bridge_pin,
                private_key=bridge_key,
                bridge_manifest_sha256=manifest.manifest_sha256,
                launch_evidence=evidence,
                launch_evidence_sha256=evidence.launch_evidence_sha256,
                signed_at=evidence.observed_at,
            ),
        )
    if outcome_path.exists():
        # resumed after the workload completed: reload the signed launch facts
        launch_receipt = ladder.load_or_build(
            "launch-receipt", _launch_receipt_model(), _absent("launch-receipt")
        )
        identity = ladder.load_or_build(
            "executor-identity", ExternalExecutorIdentity, _absent("executor-identity")
        )
    else:
        launch_receipt = receipt
    _clock_note("accepting launch", snapshot.attempt_id)
    launch = allocator.accept_external_runtime_launch(
        attempt_id=snapshot.attempt_id,
        lease_token=lease_token,
        fencing_epoch=snapshot.fencing_epoch,
        launch_receipt=launch_receipt,
    )
    if not launch.replayed and launch.snapshot.status != "running":
        _fail(f"launch acceptance left the attempt in {launch.snapshot.status}")

    # ---- workload exit facts (observed once; persisted for resume) --------
    if process is not None:
        stdout_bytes, stderr_bytes = process.communicate()
        exit_code = process.returncode
        ended_at = _utc_now()
        ended_monotonic_ns = time.monotonic_ns()
        _write_once(ladder.root / "workload-stdout.bin", stdout_bytes)
        _write_once(ladder.root / "workload-stderr.bin", stderr_bytes)
        _write_once(
            outcome_path,
            json.dumps(
                {
                    "exit_code": exit_code,
                    "ended_at": ended_at.isoformat(),
                    "ended_monotonic_ns": ended_monotonic_ns,
                },
                sort_keys=True,
                indent=2,
            ).encode("utf-8"),
        )
    else:
        outcome = json.loads(_read_bytes(outcome_path))
        exit_code = outcome["exit_code"]
        ended_at = datetime.fromisoformat(outcome["ended_at"])
        ended_monotonic_ns = outcome["ended_monotonic_ns"]
        stdout_bytes = _read_bytes(ladder.root / "workload-stdout.bin")

    # ---- termination ------------------------------------------------------
    termination_evidence = ladder.load_or_build(
        "termination-evidence",
        ExternalTerminationEvidence,
        lambda: ExternalTerminationEvidence(
            preparation_sha256=preparation.preparation_sha256,
            external_launch_receipt_sha256=launch_receipt.launch_receipt_sha256,
            executor_identity_sha256=identity.executor_identity_sha256,
            exit_code=exit_code,
            ended_at=ended_at,
            ended_monotonic_ns=ended_monotonic_ns,
            result_content_sha256=_sha256_bytes(stdout_bytes),
            termination_journal_sha256=_canonical_digest(
                {
                    "argv": launch_spec["argv"],
                    "exit_code": exit_code,
                    "result_content_sha256": _sha256_bytes(stdout_bytes),
                    "ended_at": ended_at.isoformat(),
                }
            ),
        ),
    )
    _clock_note("challenging termination", snapshot.attempt_id)
    challenged = allocator.issue_external_termination_challenge(
        attempt_id=snapshot.attempt_id,
        lease_token=lease_token,
        fencing_epoch=snapshot.fencing_epoch,
        termination_evidence=termination_evidence,
    )
    termination_receipt = ladder.load_or_build(
        "termination-receipt",
        _termination_receipt_model(),
        lambda: issue_external_runtime_termination_receipt(
            bridge_pin=bridge_pin,
            private_key=bridge_key,
            bridge_manifest_sha256=manifest.manifest_sha256,
            challenge_sha256=challenged.challenge.challenge_sha256,
            runtime_preparation_sha256=preparation.preparation_sha256,
            external_runtime_launch_receipt_sha256=launch_receipt.launch_receipt_sha256,
            runtime_launch_authorization_request_sha256=request.request_sha256,
            external_launch_authorization_sha256=(
                start.launch_authorization.authorization_sha256
            ),
            termination_evidence=termination_evidence,
            termination_evidence_sha256=termination_evidence.termination_evidence_sha256,
            signed_at=_utc_now(),
            expires_at=challenged.challenge.artifact_submission_deadline,
        ),
    )
    _clock_note("accepting termination", snapshot.attempt_id)
    termination = allocator.accept_external_runtime_termination(
        attempt_id=snapshot.attempt_id,
        lease_token=lease_token,
        fencing_epoch=snapshot.fencing_epoch,
        termination_receipt=termination_receipt,
    )
    if not termination.replayed and termination.snapshot.status != "verifying":
        _fail(
            f"termination acceptance left the attempt in {termination.snapshot.status}"
        )

    # ---- terminal artifacts ------------------------------------------------
    produced_files = sorted(
        (
            path
            for path in workload_output.rglob("*")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda path: str(path.relative_to(workload_output)),
    )
    tree = [
        {
            "path": str(path.relative_to(workload_output)),
            "content_sha256": _sha256_bytes(_read_bytes(path)),
        }
        for path in produced_files
    ]
    artifact_manifest = ladder.load_or_build(
        "artifact-manifest",
        ArtifactManifest,
        lambda: ArtifactManifest(
            intent_sha256=snapshot.intent_sha256,
            execution_id=snapshot.execution_id,
            replicate_slot_id=intent.replicate_slot.replicate_slot_id,
            infrastructure_attempt_id=snapshot.attempt_id,
            entries=tuple(
                ArtifactManifestEntry(
                    expected_artifact_id=f"art_{item['content_sha256'][:32]}",
                    artifact_key=item["path"],
                    role="raw_output",
                    content_sha256=item["content_sha256"],
                    bytes=produced_files[index].stat().st_size,
                    media_type=mimetypes.guess_type(item["path"])[0]
                    or "application/octet-stream",
                    schema_sha256=None,
                    quarantine_ref="quarantine:none",
                )
                for index, item in enumerate(tree)
            ),
            produced_at=ended_at,
        ),
    )
    receipts = tuple(
        sorted(
            (
                ArtifactVerifiedReceipt(
                    artifact_manifest_sha256=artifact_manifest.manifest_sha256,
                    producer_attempt_id=snapshot.attempt_id,
                    artifact=entry,
                    custody_mode="central_rehash",
                    verifier_principal_id=reader.artifact_verifier_principal_id,
                    object_store_id=reader.artifact_object_store_id,
                    final_object_ref=f"bridge://{manifest.node_id}/{entry.artifact_key}",
                    final_object_version=entry.content_sha256,
                    custody_receipt_sha256s=(
                        _sha256_bytes(
                            f"bridge-custody:{snapshot.attempt_id}:{entry.content_sha256}".encode()
                        ),
                    ),
                    verified_at=ended_at,
                )
                for entry in artifact_manifest.entries
            ),
            key=lambda item: item.verified_receipt_sha256,
        )
    )
    disposition = recompute_external_disposition(
        exit_code=exit_code,
        deadline_exceeded=False,
        required_artifacts_present=bool(artifact_manifest.entries),
    )
    submission = ladder.load_or_build(
        "terminal-submission",
        _submission_model(),
        lambda: issue_external_qualification_terminal_submission(
            bridge_pin=bridge_pin,
            private_key=bridge_key,
            expected_disposition=disposition,
            bridge_manifest_sha256=manifest.manifest_sha256,
            intent_sha256=snapshot.intent_sha256,
            execution_id=snapshot.execution_id,
            attempt_id=snapshot.attempt_id,
            resource_lease_sha256=snapshot.resource_lease_sha256,
            fencing_epoch=snapshot.fencing_epoch,
            lease_token_sha256=snapshot.lease_token_sha256,
            accepted_external_runtime_termination_sha256=(
                termination.accepted_termination.accepted_termination_sha256
            ),
            artifact_manifest_sha256=artifact_manifest.manifest_sha256,
            output_tree_sha256=_canonical_digest(tree),
            artifact_verified_receipt_sha256s=tuple(
                item.verified_receipt_sha256 for item in receipts
            ),
            disposition=disposition,
            submitted_at=_utc_now(),
        ),
    )
    _clock_note("accepting terminal artifacts", snapshot.attempt_id)
    allocator.accept_external_terminal_artifacts(
        attempt_id=snapshot.attempt_id,
        lease_token=lease_token,
        fencing_epoch=snapshot.fencing_epoch,
        terminal_submission=submission,
        artifact_manifest=artifact_manifest,
        artifact_verified_receipts=receipts,
    )
    _clock_note("settling", snapshot.attempt_id)
    pending = allocator.pull_pending_external_qualification_terminal_settlement(
        bridge_manifest_sha256=manifest.manifest_sha256,
    )
    settled = None
    if pending is not None:
        settled = allocator.settle_external_qualification_terminal(
            terminal_acceptance=pending,
        )
        if settled.snapshot.status != "succeeded":
            _fail(f"settlement left the attempt in {settled.snapshot.status}")
    status = settled.snapshot.status if settled is not None else claim.snapshot.status
    if status != "succeeded":
        _fail(f"attempt {snapshot.attempt_id} did not settle (status {status})")

    # ---- reader verification (the W1 evidence seam) ------------------------
    source = allocator.load_verified_qualification_terminal_source(
        execution_id=snapshot.execution_id,
        attempt_id=snapshot.attempt_id,
    )
    lineage = allocator.load_verified_external_qualification_run_lineage(
        execution_id=snapshot.execution_id,
        attempt_id=snapshot.attempt_id,
        observed_at=_utc_now(),
    )
    material = allocator.load_verified_external_qualification_raw_run_material(
        execution_id=snapshot.execution_id,
        attempt_id=snapshot.attempt_id,
        observed_at=_utc_now(),
    )
    outbox = allocator.list_qualification_terminal_outbox(
        attempt_id_allowlist=(snapshot.attempt_id,)
    )
    if source is None or lineage is None or material is None:
        _fail("settled attempt is missing its verified reader export")
    if (
        source.terminal_authority_sha256 != submission.terminal_submission_sha256
        or lineage.terminal_acceptance_sha256 != source.terminal_authority_sha256
        or material.accepted_terminal_submission.terminal_authority_sha256
        != source.terminal_authority_sha256
        or source.lineage_evidence_sha256 != lineage.lineage_sha256
        or len(outbox) != 1
        or outbox[0].terminal_authority_sha256 != source.terminal_authority_sha256
    ):
        _fail("verified reader exports disagree with the settled terminal acceptance")
    evidence = {
        "schema_name": EVIDENCE_SCHEMA,
        "schema_version": 1,
        "attempt_id": snapshot.attempt_id,
        "execution_id": snapshot.execution_id,
        "status": status,
        "disposition": disposition,
        "charged_microunits": termination.charged_microunits,
        "lease_seconds": int((ended_at - identity.started_at).total_seconds()),
        "terminal_authority_sha256": source.terminal_authority_sha256,
        "outbox_id": outbox[0].outbox_id,
        "outbox_authority_kind": outbox[0].terminal_authority_kind,
        "lineage_sha256": lineage.lineage_sha256,
        "material_sha256": material.material_sha256,
        "source_lineage_evidence_sha256": source.lineage_evidence_sha256,
        "artifact_manifest_sha256": artifact_manifest.manifest_sha256,
        "artifact_count": len(artifact_manifest.entries),
        "workload": {
            "argv": launch_spec["argv"],
            "exit_code": exit_code,
            "executor_started_at": identity.started_at.isoformat(),
            "runtime_ended_at": ended_at.isoformat(),
        },
        "dispatch_records": str(ladder.root),
    }
    if args.evidence is not None:
        Path(args.evidence).write_text(
            json.dumps(evidence, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    print("DISPATCH_EVIDENCE " + json.dumps(evidence, sort_keys=True))
    return 0


def _launch_receipt_model():
    from aletheia.execution.external_bridge_contracts import (
        ExternalRuntimeLaunchReceipt,
    )

    return ExternalRuntimeLaunchReceipt


def _termination_receipt_model():
    from aletheia.execution.external_bridge_contracts import (
        ExternalRuntimeTerminationReceipt,
    )

    return ExternalRuntimeTerminationReceipt


def _submission_model():
    from aletheia.execution.external_bridge_contracts import (
        ExternalQualificationTerminalSubmission,
    )

    return ExternalQualificationTerminalSubmission


def _clock_note(stage: str, attempt_id: str) -> None:
    moment = datetime.now(timezone.utc)
    print(f"[{moment.isoformat()}] {stage} ({attempt_id})", file=sys.stderr)


# --------------------------------------------------------------------------
# Adjudication verb
# --------------------------------------------------------------------------


def _adjudicate(allocator, bridge_authority) -> int:
    adjudicated = allocator.adjudicate_expired_external_qualification_terminal(
        bridge_manifest_sha256=bridge_authority.manifest.manifest_sha256,
    )
    if adjudicated is None:
        print("ADJUDICATED none")
        return 0
    print(
        "ADJUDICATED "
        + json.dumps(
            {
                "attempt_id": adjudicated.snapshot.attempt_id,
                "execution_id": adjudicated.snapshot.execution_id,
                "status": adjudicated.snapshot.status,
                "expiration_sha256": adjudicated.terminal_expiration.expiration_sha256,
                "outbox_id": adjudicated.outbox_id,
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.acknowledge != ACKNOWLEDGEMENT:
        _fail(f"--acknowledge must be exactly {ACKNOWLEDGEMENT}")

    state_path = Path(args.deployment_state).resolve(strict=True)
    state = json.loads(_read_bytes(state_path))
    if state.get("schema_name") != STATE_SCHEMA:
        _fail(f"{state_path} is not an ARL-2 deployment state file")
    if _sha256_bytes(args.database_url.encode("utf-8")) != state["database_url_sha256"]:
        _fail("database URL sha differs from the commissioned value")

    os.environ["ALETHEIA_DATABASE_URL"] = args.database_url
    release_root = Path(__file__).resolve().parent.parent
    if str(release_root) not in sys.path:
        sys.path.insert(0, str(release_root))

    allocator, reader, bridge_authority, bridge_key = _compose_allocator(
        state, state_path, args
    )
    if args.adjudicate:
        return _adjudicate(allocator, bridge_authority)
    return _dispatch(allocator, reader, bridge_authority, bridge_key, args)


if __name__ == "__main__":
    raise SystemExit(main())
