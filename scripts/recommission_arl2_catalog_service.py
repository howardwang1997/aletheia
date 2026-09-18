#!/usr/bin/env python3
"""Re-freeze one live-keyed ARL-2 catalog service config against the commissioning state.

The three live-keyed services (protocol_compilation, execution_authorization,
independent_validation) are deferred at commissioning: their service identity
(receipt key, socket, principals, service_pin bytes) is commissioned inside the
worker composition, but their composition configs - the ones embedding the
per-round catalogs - are authored at each per-round pause by this script.

Every identity value is byte-preserved from configs/arl2-deployment-state.json
and the worker composition file: the embedded ControllerWorkerRPCServicePin is
lifted from the worker config, so pin_sha256 and service_id never move. Only
the catalog payload changes, plus the derived composition_config_file_sha256,
deployment_sha256, and runtime_id on the re-issued deployment.

prepared_at stays at the commissioning T0 for all three services. The F9-v2 and
execution-authorization configs lockstep their embedded reader/custody
prepared_at to the config prepared_at (f9_v2_validation_runtime.py:248,
execution_authorization_runtime.py:182), and those custody documents are frozen
commissioning artifacts, so a re-freeze reusing their exact bytes must keep T0;
the unchanged pin window still covers it. Protocol compilation has no such
coupling, and T0 satisfies its deployment window gate too.

For independent_validation the script also runs the OFFLINE LOOKUP ASSERTION:
with --envelope it recomputes the 16-field lookup from the exported raw-run
envelope (f9_v2_assessor.py:257-285) and refuses unless the new catalog holds
exactly one template with that lookup sha AND the envelope's admission policy
matches the template the way assessment time will check it
(f9_v2_assessor.py:225-235). The validation archive is write-once, so a wrong
first COMMIT_VALIDATION lookup is permanently fatal; this assertion is the one
place a typo is cheap.

Run as root on the commissioning host (the config is written service-owned, the
deployment driver-owned, mirroring author-arl2-deployments.py). Generation 0
writes the conventional first-authoring names; a later re-freeze passes
--generation N so superseded files stay untouched on disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_SCHEMA = "aletheia.arl2_deployment_state"

DEFERRED_FACTORIES = {
    "protocol_compilation": (
        "aletheia.research_controller_protocol_compilation_runtime",
        "build_protocol_compilation_rpc_service",
    ),
    "execution_authorization": (
        "aletheia.research_controller_execution_authorization_runtime",
        "build_execution_authorization_rpc_service",
    ),
    "independent_validation": (
        "aletheia.research_controller_f9_v2_validation_runtime",
        "build_f9_v2_validation_rpc_service",
    ),
}

SERVICE_CHOICES = tuple(DEFERRED_FACTORIES)


def _fail(message: str) -> None:
    print(f"recommission_arl2_catalog_service: {message}", file=sys.stderr)
    raise SystemExit(1)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        _fail(f"cannot read {path}: {exc}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path):
    try:
        return json.loads(_read_bytes(path), object_pairs_hook=_unique_object)
    except (TypeError, ValueError) as exc:
        _fail(f"{path} is not duplicate-free JSON: {exc}")


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str, label: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        _fail(f"{label} is not an ISO timestamp: {exc}")
    if moment.tzinfo is None or moment.utcoffset() is None:
        _fail(f"{label} is not timezone-aware")
    return moment.astimezone(timezone.utc)


def _write_new_file(path: Path, payload: bytes, *, mode: int, uid: int, gid: int) -> str:
    if path.exists():
        _fail(f"{path} already exists (write-once discipline); pass a fresh --generation")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
    os.chown(path, uid, gid)
    os.chmod(path, mode)
    return _sha256_bytes(payload)


def _stat_directory(path: Path) -> dict[str, int]:
    try:
        metadata = os.stat(path)
    except OSError as exc:
        _fail(f"cannot stat {path}: {exc}")
    if not stat.S_ISDIR(metadata.st_mode):
        _fail(f"{path} is not a directory")
    return {
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "device_id": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _derive_public_hex(private_path: Path) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    material = _read_bytes(private_path)
    if len(material) != 32:
        _fail(f"{private_path} is not a raw 32-byte Ed25519 private key")
    return (
        Ed25519PrivateKey.from_private_bytes(material)
        .public_key()
        .public_bytes_raw()
        .hex()
    )


def _load_templates(paths: list[Path], model_class):
    """Parse each template file: a bare template object or the authoring wrapper."""

    templates = []
    for path in paths:
        payload = _load_json(path)
        if isinstance(payload, dict) and "template" in payload and "schema_name" not in payload:
            payload = payload["template"]
        try:
            templates.append(model_class.model_validate(payload))
        except (TypeError, ValueError) as exc:
            _fail(f"{path} does not hold a valid {model_class.__name__}: {exc}")
    if not templates:
        _fail("at least one --template-file is required")
    return tuple(templates)


def _assert_window(label: str, valid_from: datetime, active_until: datetime, now: datetime) -> None:
    if not valid_from <= now < active_until:
        _fail(f"{label} window [{_iso(valid_from)}, {_iso(active_until)}) does not contain now")


def _assert_binding_matches_pin(label: str, binding, pin) -> None:
    """Mirror the compose-time authority closure offline (principal/key/policy)."""

    if (
        binding.principal_id != pin.principal_id
        or binding.key_id != pin.key_id
        or binding.policy_sha256 != pin.policy_sha256
    ):
        _fail(f"{label}: state authority binding differs from the commissioned bridge pin")


def _offline_lookup_assertion(*, envelope_path: Path, catalog, manifest_sha: str, now: datetime):
    """Recompute the 16-field lookup and pre-check the assessment-time gates."""

    from aletheia.observations.f9_v2_validation import (
        RawRunEnvelope,
        build_f9_v2_validation_request,
    )
    from aletheia.research_kernel.schemas import canonical_sha256

    envelope = RawRunEnvelope.model_validate(_load_json(envelope_path))
    request = build_f9_v2_validation_request(raw_run=envelope, requested_at=now)
    if request.validator_manifest_sha256 != manifest_sha:
        _fail("envelope validator manifest differs from the independent_validation binding")
    authorization = envelope.scientific_authorization.message
    artifact_binding = authorization.scientific_observation_artifact_binding
    lookup_payload = {
        "schema_name": "aletheia.f9_v2_exact_content_assessment_lookup",
        "schema_version": 1,
        "quest_id": request.quest_id,
        "action_sha256": request.action_sha256,
        "action_authorized_event_sha256": request.action_authorized_event_sha256,
        "authorized_graph_snapshot_sha256": request.authorized_graph_snapshot_sha256,
        "graph_scope_sha256": request.graph_scope_sha256,
        "protocol_sha256": request.protocol_sha256,
        "world_model_snapshot_sha256": request.world_model_snapshot_sha256,
        "scientific_slot_id": request.scientific_slot_id,
        "scientific_observation_artifact_binding_sha256": (
            request.scientific_observation_artifact_binding_sha256
        ),
        "artifact_key": request.artifact_key,
        "raw_observation_schema_sha256": artifact_binding.expected_artifact.schema_sha256,
        "raw_observation_content_sha256": request.raw_observation_content_sha256,
        "raw_observation_bytes": request.raw_observation_bytes,
        "raw_observation_media_type": request.raw_observation_media_type,
        "validator_manifest_sha256": request.validator_manifest_sha256,
        "observation_validation_policy_sha256": request.observation_validation_policy_sha256,
    }
    lookup_sha = canonical_sha256(lookup_payload)
    matches = [item for item in catalog.templates if item.lookup_sha256 == lookup_sha]
    if len(matches) != 1:
        _fail(
            f"OFFLINE LOOKUP ASSERTION failed: {len(matches)} catalog templates match the "
            "envelope's recomputed lookup sha; the write-once archive would reject this "
            "COMMIT_VALIDATION call"
        )
    template = matches[0]
    admission_policy = authorization.admission_policy
    mapped_bins = {item.outcome_bin_id for item in admission_policy.outcome_bin_mappings}
    if (
        admission_policy.validator_manifest_sha256 != request.validator_manifest_sha256
        or admission_policy.observation_validation_policy_sha256
        != request.observation_validation_policy_sha256
        or admission_policy.analysis_outcome_space_sha256 != request.analysis_outcome_space_sha256
        or template.outcome_bin_id not in mapped_bins
    ):
        _fail("envelope admission policy does not close over the matched template")
    return lookup_sha


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-freeze one deferred ARL-2 catalog service (config + deployment)."
    )
    parser.add_argument(
        "--service",
        required=True,
        choices=SERVICE_CHOICES,
        help="the deferred live-keyed service to re-freeze",
    )
    parser.add_argument(
        "--deployment-state",
        required=True,
        help="absolute path to configs/arl2-deployment-state.json from the commissioning window",
    )
    parser.add_argument(
        "--release-root",
        required=True,
        help="absolute path to the staged release checkout (reviewed_code_root)",
    )
    parser.add_argument(
        "--template-file",
        action="append",
        required=True,
        dest="template_files",
        help="path to one authored catalog template JSON (repeatable); the authoring "
        "wrapper {'template': ...} shape is also accepted",
    )
    parser.add_argument(
        "--database-url",
        required=True,
        help="the campaign database URL; its sha must equal the commissioned value",
    )
    parser.add_argument(
        "--envelope",
        help="exported RawRunEnvelope JSON for the OFFLINE LOOKUP ASSERTION "
        "(required for independent_validation)",
    )
    parser.add_argument(
        "--catalog-id",
        help="catalog_id for the F9-v2 assessment catalog (default: derived from the "
        "window id and generation)",
    )
    parser.add_argument(
        "--generation",
        type=int,
        default=0,
        help="re-freeze generation; 0 writes the first-authoring file names, N>0 writes "
        "suffixed names so superseded files stay untouched",
    )
    args = parser.parse_args()

    if os.geteuid() != 0:
        _fail("this script must run as root (custody handover to service/driver owners)")

    try:
        state_path = Path(args.deployment_state).resolve(strict=True)
        release = Path(args.release_root).resolve(strict=True)
    except OSError as exc:
        _fail(f"cannot resolve an input path: {exc}")
    state = _load_json(state_path)
    if state.get("schema_name") != STATE_SCHEMA:
        _fail(f"{state_path} is not an ARL-2 deployment state file")
    if args.service not in state.get("deferred_services", []):
        _fail(f"{args.service} is not a deferred service of window {state.get('window_id')}")
    if args.generation < 0:
        _fail("--generation must be >= 0")

    working = state_path.parent.parent
    prepared_at = _parse_iso(state["prepared_at"], "state prepared_at")
    valid_from = _parse_iso(state["valid_from"], "state valid_from")
    expires_at = _parse_iso(state["expires_at"], "state expires_at")
    now = datetime.now(timezone.utc)
    _assert_window("commissioning key", valid_from, expires_at, now)

    database_sha = _sha256_bytes(args.database_url.encode("utf-8"))
    if database_sha != state["database_url_sha256"]:
        _fail("database URL sha differs from the commissioned value")

    # ---- the commissioned worker composition carries the byte-preserved pin --
    from aletheia.research_controller.worker_composition import (
        ControllerWorkerCompositionError,
        load_research_controller_worker_runtime_config,
    )
    from aletheia.research_kernel.schemas import canonical_json_bytes

    worker_path = Path(state["worker"]["configuration_path"])
    worker_bytes = _read_bytes(worker_path)
    if _sha256_bytes(worker_bytes) != state["worker"]["configuration_file_sha256"]:
        _fail("worker composition bytes differ from the state file pin")
    try:
        worker = load_research_controller_worker_runtime_config(worker_bytes)
    except (ControllerWorkerCompositionError, TypeError, ValueError) as exc:
        _fail(f"worker composition no longer parses: {exc}")

    pin = getattr(worker.rpc_services, args.service)
    kernel_reader = worker.kernel_reader
    terminal_reader = worker.terminal_reader
    reader_json = terminal_reader.model_dump(mode="json")
    # The state file was written through canonical_json_bytes, which strips
    # None-valued dict entries; model_dump emits them. Compare canonical forms.
    if canonical_json_bytes(reader_json) != canonical_json_bytes(
        state["qualification"]["reader"]
    ):
        _fail("worker terminal reader differs from the state file's qualification reader")
    if _parse_iso(reader_json["prepared_at"], "reader prepared_at") != prepared_at:
        _fail("worker terminal reader prepared_at is not the commissioning T0")

    # Custody drift check: the CAS the services pin must still be the commissioned one.
    cas_stat = _stat_directory(Path(kernel_reader.cas_root))
    if (
        cas_stat["uid"] != kernel_reader.cas_owner_uid
        or cas_stat["gid"] != kernel_reader.cas_group_gid
        or cas_stat["device_id"] != kernel_reader.cas_device_id
        or cas_stat["inode"] != kernel_reader.cas_inode
        or cas_stat["mode"] != kernel_reader.cas_directory_mode
    ):
        _fail("Kernel CAS custody drifted from the commissioned kernel_reader pin")

    from aletheia.research_controller.step_executor import ControllerStepAuthorityBinding

    try:
        binding = ControllerStepAuthorityBinding.model_validate(
            state["bindings"][args.service]["json"]
        )
    except (TypeError, ValueError) as exc:
        _fail(f"state binding for {args.service} is invalid: {exc}")
    if binding.binding_sha256 != state["bindings"][args.service]["sha256"]:
        _fail(f"state binding sha mismatch for {args.service}")

    service_dir = args.service.replace("_", "-")
    suffix = "" if args.generation == 0 else f".g{args.generation}"
    config_path = working / "configs" / "services" / f"{service_dir}{suffix}.json"
    deployment_path = working / "configs" / "services" / f"{service_dir}-deployment{suffix}.json"

    # All three runtime config classes are function-local inside their
    # factories (only the factory is exported), so every config is authored as
    # a canonical dict; nested models are validated by construction here and
    # the closure validators run at service compose time. The dict's
    # prepared_at must be the exact string pydantic emitted for the same
    # instant; the reader dump provides it.
    header = {
        "controller_id": state["controller"]["controller_id"],
        "controller_manifest_sha256": state["controller"]["manifest_sha256"],
        "worker_process_principal_id": worker.process_principal_id,
        "service_id": pin.service_id,
        "service_pin_sha256": pin.pin_sha256,
        "database_url_sha256": database_sha,
        "schema_revision": state["schema_revision"],
        "kernel_reader": kernel_reader.model_dump(mode="json"),
        "authority_binding": binding.model_dump(mode="json"),
        "prepared_at": reader_json["prepared_at"],
    }

    receipt_key_path = working / "keys" / f"{service_dir}-receipt" / "receipt.key"
    receipt_public_hex = _derive_public_hex(receipt_key_path)
    if receipt_public_hex != pin.receipt_public_key_ed25519_hex:
        _fail(f"{receipt_key_path} does not derive the pinned receipt public key")

    catalog = None
    config = None

    if args.service == "protocol_compilation":
        from aletheia.research_controller.protocol_compilation_step import (
            ProtocolCompilationPolicyPin,
        )
        from aletheia.research_controller.protocol_template_provider import (
            FrozenProtocolCompilationTemplate,
            FrozenProtocolTemplateProviderPolicyPin,
        )

        templates = _load_templates(
            [Path(p) for p in args.template_files], FrozenProtocolCompilationTemplate
        )
        policy = ProtocolCompilationPolicyPin.model_validate(state["policy_pins"]["compilation"])
        authors = {item.request.protocol.authored_by_principal_id for item in templates}
        if len(authors) != 1:
            _fail("protocol templates name more than one authored_by principal")
        prepared_by = authors.pop()
        if prepared_by not in policy.allowed_protocol_author_principal_ids:
            _fail(f"protocol author {prepared_by} is not allowed by the compilation policy")
        for item in templates:
            request = item.request
            if (
                request.capability_catalog.catalog_sha256 != policy.capability_catalog_sha256
                or request.resource_catalog.catalog_sha256 != policy.resource_catalog_sha256
                or request.compiler_implementation_sha256 != policy.compiler_implementation_sha256
            ):
                _fail(f"template for action {item.action_sha256} escapes the compilation policy")
        if binding.policy_sha256 != policy.policy_sha256:
            _fail("state compilation binding does not close over the compilation policy")

        provider_source = (
            release / "aletheia" / "research_controller" / "protocol_template_provider.py"
        )
        provider_sha = _sha256_bytes(_read_bytes(provider_source))
        provider_policy = FrozenProtocolTemplateProviderPolicyPin(
            provider_implementation_sha256=provider_sha,
            compilation_policy_sha256=policy.policy_sha256,
            prepared_by_principal_id=prepared_by,
            templates=tuple(sorted(templates, key=lambda item: (
                item.action_sha256,
                item.action_kind.value,
                item.request_sha256,
            ))),
        )
        # merged round-split channel (Q13(b)): when the deployment state pins
        # the campaign request bytes, the compile config carries the pin so
        # the deployed service loads the commissioning-time bindings and runs
        # the merged gate. Absent pin (older states, or a policy that carries
        # its own round_split_binding) leaves the fields out; the config
        # validator rejects a request pin combined with a bound policy.
        request_entry = state.get("request") or {}
        campaign_request_fields = (
            {
                "campaign_request_path": request_entry["request_path"],
                "campaign_request_file_sha256": request_entry["request_file_sha256"],
            }
            if request_entry.get("request_path") and request_entry.get("request_file_sha256")
            else {}
        )
        config = {
            "schema_name": "aletheia.protocol_compilation_rpc_service_config",
            "schema_version": 1,
            **header,
            **campaign_request_fields,
            "compilation_policy": policy.model_dump(mode="json"),
            "provider_policy": provider_policy.model_dump(mode="json"),
            "provider_implementation_source_path": str(provider_source),
            "provider_implementation_source_sha256": provider_sha,
            "direct_scientific_authority": False,
            "kernel_signing_key_loaded": False,
            "observation_signing_key_loaded": False,
            "execution_access_allowed": False,
            "generic_model_callback_allowed": False,
            "dynamic_template_mutation_allowed": False,
        }

    elif args.service == "execution_authorization":
        from aletheia.execution.qualification_custody import QualificationPreAdmissionCustodyConfig
        from aletheia.observations.scientific_bridge import ScientificBridgeAuthorityPin
        from aletheia.research_controller.execution_authorization_service import (
            FrozenScientificExecutionAuthorizationCatalog,
            FrozenScientificExecutionAuthorizationTemplate,
        )

        templates = _load_templates(
            [Path(p) for p in args.template_files], FrozenScientificExecutionAuthorizationTemplate
        )
        custody = QualificationPreAdmissionCustodyConfig.model_validate(
            state["qualification"]["custody"]
        )
        if custody.prepared_at != prepared_at:
            _fail("qualification custody prepared_at differs from the commissioning T0")
        execution_pin = ScientificBridgeAuthorityPin.model_validate(state["bridge"]["execution"])
        _assert_binding_matches_pin("execution_authorization", binding, execution_pin)
        issuer_source = (
            release / "aletheia" / "research_controller" / "execution_authorization_service.py"
        )
        issuer_sha = _sha256_bytes(_read_bytes(issuer_source))
        signing_key_path = working / "keys" / "execution-authorization-execution" / "execution.key"
        signing_public_hex = _derive_public_hex(signing_key_path)
        if signing_public_hex != execution_pin.public_key_ed25519_hex:
            _fail(f"{signing_key_path} does not derive the execution bridge public key")

        catalog = FrozenScientificExecutionAuthorizationCatalog(
            issuer_implementation_sha256=issuer_sha,
            qualification_authority_pin=custody.qualification_authority_pin,
            execution_authority_pin=execution_pin,
            validator_authority_pin=ScientificBridgeAuthorityPin.model_validate(
                state["bridge"]["validator"]
            ),
            admission_authority_pin=ScientificBridgeAuthorityPin.model_validate(
                state["bridge"]["admission"]
            ),
            templates=tuple(sorted(templates, key=lambda item: (
                item.action_sha256,
                item.compilation_sha256,
                item.template_sha256,
            ))),
        )
        config = {
            "schema_name": "aletheia.execution_authorization_rpc_service_config",
            "schema_version": 1,
            **header,
            "qualification_custody": custody.model_dump(mode="json"),
            "authorization_catalog": catalog.model_dump(mode="json"),
            "issuer_implementation_source_path": str(issuer_source),
            "issuer_implementation_source_sha256": issuer_sha,
            "execution_signing_key": {
                "path": str(signing_key_path),
                "file_sha256": _sha256_bytes(_read_bytes(signing_key_path)),
                "key_id": execution_pin.key_id,
                "owner_uid": pin.peer_uid,
                "owner_gid": pin.peer_gid,
                "file_mode": 0o400,
            },
            "direct_kernel_mutation_allowed": False,
            "execution_launch_allowed": False,
            "qualification_admission_allowed": False,
            "direct_observation_admission_allowed": False,
            "validator_signing_key_loaded": False,
            "admission_signing_key_loaded": False,
            "kernel_signing_key_loaded": False,
            "dynamic_template_mutation_allowed": False,
        }

    else:  # independent_validation
        if not args.envelope:
            _fail("independent_validation requires --envelope for the offline lookup assertion")
        from aletheia.observations.f9_v2_assessor import (
            FrozenF9V2ExactContentAssessmentCatalog,
            FrozenF9V2ExactContentAssessmentTemplate,
        )
        from aletheia.observations.scientific_bridge import (
            ObservationDatabaseAuthorityPin,
            ScientificBridgeAuthorityPin,
            VerifiedExecutionAuthorityProjection,
        )

        templates = _load_templates(
            [Path(p) for p in args.template_files],
            FrozenF9V2ExactContentAssessmentTemplate,
        )
        manifest_sha = binding.service_manifest_sha256
        for item in templates:
            if item.validator_manifest_sha256 != manifest_sha:
                _fail(
                    f"template {item.template_sha256} does not carry the "
                    "independent_validation identity manifest sha"
                )
        assessor_source = release / "aletheia" / "observations" / "f9_v2_assessor.py"
        assessor_sha = _sha256_bytes(_read_bytes(assessor_source))
        # The config's service pin is the DOMAIN module the runtime imports
        # (f9_v2_validation_runtime.py:459 gates on its __file__), not the RPC
        # factory module; the factory is pinned separately on the deployment.
        service_source = release / "aletheia" / "observations" / "f9_v2_validation.py"
        service_sha = _sha256_bytes(_read_bytes(service_source))
        catalog_id = args.catalog_id or f"f9v2:{state['window_id']}:g{args.generation}"
        catalog = FrozenF9V2ExactContentAssessmentCatalog(
            catalog_id=catalog_id,
            assessor_implementation_sha256=assessor_sha,
            templates=tuple(sorted(templates, key=lambda item: item.template_sha256)),
        )

        archive_root = working / "f9-v2-validation-archive"
        archive_stat = _stat_directory(archive_root)
        if archive_stat["uid"] != pin.peer_uid:
            _fail("validation archive root is not owned by the independent_validation uid")
        if archive_stat["mode"] not in (0o700, 0o750):
            _fail("validation archive root mode is outside {0o700, 0o750}")
        validator_pin = ScientificBridgeAuthorityPin.model_validate(state["bridge"]["validator"])
        _assert_binding_matches_pin("independent_validation", binding, validator_pin)
        validator_key_path = (
            working / "keys" / "independent-validation-validator" / "validator.key"
        )
        validator_public_hex = _derive_public_hex(validator_key_path)
        if validator_public_hex != validator_pin.public_key_ed25519_hex:
            _fail(f"{validator_key_path} does not derive the validator bridge public key")

        artifact_policy_sha = state["qualification"]["policies"]["artifact-verification"]
        artifact_authority = VerifiedExecutionAuthorityProjection(
            principal_id=reader_json["artifact_verifier_principal_id"],
            key_id=artifact_policy_sha,
            policy_sha256=artifact_policy_sha,
        )

        # IndependentF9V2ValidationRPCConfig is function-local inside its factory
        # (research_controller_f9_v2_validation_runtime.py:116-157), so the config
        # is authored as a canonical dict. Every nested model above was validated
        # by construction; the authority-closure validator runs at compose time.
        config = {
            "schema_name": "aletheia.independent_f9_v2_validation_rpc_service_config",
            "schema_version": 1,
            "controller_id": header["controller_id"],
            "controller_manifest_sha256": header["controller_manifest_sha256"],
            "worker_process_principal_id": header["worker_process_principal_id"],
            "service_id": header["service_id"],
            "service_pin_sha256": header["service_pin_sha256"],
            "database_url_sha256": database_sha,
            "schema_revision": header["schema_revision"],
            "kernel_reader": kernel_reader.model_dump(mode="json"),
            "authority_binding": binding.model_dump(mode="json"),
            "validator_authority_pin": validator_pin.model_dump(mode="json"),
            "validator_signing_key": {
                "path": str(validator_key_path),
                "file_sha256": _sha256_bytes(_read_bytes(validator_key_path)),
                "key_id": validator_pin.key_id,
                "owner_uid": pin.peer_uid,
                "owner_gid": pin.peer_gid,
                "file_mode": 0o400,
            },
            "execution_authority_pin": ScientificBridgeAuthorityPin.model_validate(
                state["bridge"]["execution"]
            ).model_dump(mode="json"),
            "admission_authority_pin": ScientificBridgeAuthorityPin.model_validate(
                state["bridge"]["admission"]
            ).model_dump(mode="json"),
            "database_authority_pin": ObservationDatabaseAuthorityPin.model_validate(
                state["bridge"]["database"]
            ).model_dump(mode="json"),
            "qualification_reader": reader_json,
            "artifact_verification_authority": artifact_authority.model_dump(mode="json"),
            "validation_archive": {
                "root": str(archive_root),
                "owner_uid": archive_stat["uid"],
                "group_gid": archive_stat["gid"],
                "device_id": archive_stat["device_id"],
                "inode": archive_stat["inode"],
                "directory_mode": archive_stat["mode"],
                "validator_manifest_sha256": manifest_sha,
                "read_only": False,
                "campaign_publication_allowed": True,
            },
            "assessment_catalog": catalog.model_dump(mode="json"),
            "assessor_implementation_source_path": str(assessor_source),
            "assessor_implementation_source_sha256": assessor_sha,
            "service_implementation_source_path": str(service_source),
            "service_implementation_source_sha256": service_sha,
            "prepared_at": reader_json["prepared_at"],
            "validator_signing_key_loaded": True,
            "database_signing_key_loaded": False,
            "admission_signing_key_loaded": False,
            "execution_signing_key_loaded": False,
            "kernel_signing_key_loaded": False,
            "database_mutation_allowed": False,
            "execution_mutation_allowed": False,
            "artifact_mutation_allowed": False,
            "campaign_publication_allowed": True,
            "direct_observation_admission_allowed": False,
            "direct_kernel_mutation_allowed": False,
            "generic_model_callback_allowed": False,
        }

    # ---- validate the deployment in memory, then write both files ------------
    config_payload = canonical_json_bytes(config)
    config_sha = _sha256_bytes(config_payload)

    from aletheia.research_controller_rpc_runtime import (
        ControllerWorkerRPCServerDeployment,
        load_controller_worker_rpc_server_deployment,
    )

    socket_parent = Path(pin.socket_path).parent
    socket_stat = _stat_directory(socket_parent)
    module_name, attribute = DEFERRED_FACTORIES[args.service]
    factory_path = release / "aletheia" / f"{module_name.rsplit('.', 1)[-1]}.py"
    try:
        deployment = ControllerWorkerRPCServerDeployment(
            service_pin=pin,
            controller_id=header["controller_id"],
            controller_manifest_sha256=header["controller_manifest_sha256"],
            worker_process_principal_id=header["worker_process_principal_id"],
            worker_peer_uid=state["driver"].get("worker_role_uid", state["driver"]["uid"]),
            worker_peer_gid=state["driver"]["gid"],
            process_uid=pin.peer_uid,
            process_gid=pin.peer_gid,
            socket_parent_path=str(socket_parent),
            socket_parent_owner_uid=socket_stat["uid"],
            socket_parent_owner_gid=socket_stat["gid"],
            socket_parent_mode=socket_stat["mode"],
            socket_parent_device_id=socket_stat["device_id"],
            socket_parent_inode=socket_stat["inode"],
            receipt_private_key_path=str(receipt_key_path),
            receipt_private_key_sha256=_sha256_bytes(_read_bytes(receipt_key_path)),
            reviewed_code_root=str(release),
            composition_factory_module=module_name,
            composition_factory_attribute=attribute,
            composition_factory_source_path=str(factory_path),
            composition_factory_source_sha256=_sha256_bytes(_read_bytes(factory_path)),
            composition_config_path=str(config_path),
            composition_config_file_sha256=config_sha,
            prepared_at=prepared_at,
        )
    except (TypeError, ValueError) as exc:
        _fail(f"re-issued deployment does not validate: {exc}")
    deployment_payload = canonical_json_bytes(deployment)

    _write_new_file(
        config_path, config_payload, mode=0o440, uid=pin.peer_uid, gid=pin.peer_gid
    )
    deployment_sha = _write_new_file(
        deployment_path,
        deployment_payload,
        mode=0o644,
        uid=state["driver"]["uid"],
        gid=state["driver"]["gid"],
    )

    # ---- offline verification ----------------------------------------------
    try:
        load_controller_worker_rpc_server_deployment(
            deployment_path, expected_file_sha256=deployment_sha
        )
    except (TypeError, ValueError) as exc:
        _fail(f"re-issued deployment does not load: {exc}")
    if canonical_json_bytes(_load_json(config_path)) != config_payload:
        _fail("written config is not canonical JSON")

    lookup_sha = None
    if args.service == "independent_validation":
        lookup_sha = _offline_lookup_assertion(
            envelope_path=Path(args.envelope),
            catalog=catalog,
            manifest_sha=binding.service_manifest_sha256,
            now=now,
        )

    print(
        json.dumps(
            {
                "service": args.service,
                "generation": args.generation,
                "config_path": str(config_path),
                "config_file_sha256": config_sha,
                "deployment_path": str(deployment_path),
                "deployment_file_sha256": deployment_sha,
                "runtime_id": deployment.runtime_id,
                "deployment_sha256": deployment.deployment_sha256,
                "service_id": pin.service_id,
                "service_pin_sha256": pin.pin_sha256,
                "prepared_at": _iso(prepared_at),
                "lookup_sha256": lookup_sha,
                "runner_argv": [
                    "/opt/aletheia/python/bin/python",
                    "scripts/run_research_controller_rpc_service.py",
                    "--deployment-manifest",
                    str(deployment_path),
                    "--deployment-manifest-sha256",
                    deployment_sha,
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
