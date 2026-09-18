#!/usr/bin/env python3
"""Author the full ARL-2 commissioning deployment set (runbook W1 step 6).

One window, one invocation: eleven of the fourteen RPC service deployments
(the eight simply-keyed/keyless worker-facing services, the two
kernel-command signers, and the cuprate diagnostic), the four role
deployments, the controller manifest with its policy documents, the worker
composition (adapter set + the full eleven-service pin set), and the driver
config/deployment wiring the campaign request.

The three live-keyed services (protocol compilation, execution
authorization, F9-v2 exact-content validation) are DEFERRED to their first
per-round pause (runbook W3/W4): their catalogs bind live round material
that cannot exist at commissioning, and their frozen configs require at
least one template.  Their identity IS commissioned here - identity
manifest, policy pin, receipt key, socket directory, and the service pin
inside the worker composition - so recommission_arl2_catalog_service.py
authors each config later against the same identity values recorded in the
deployment-state file.  Nothing calls those services before their pause.

Runs ONCE per window, as root on the box (it creates service custody and
chowns it to the service identities).  An existing deployment-state file
aborts the run (abort doctrine; the quest is not reused).

Inputs, all from earlier kit scripts - nothing is hand-typed:

  --activation-state  configs/arl2-activation-state.json
                      (scripts/author-arl2-authorities.py; also reads the
                      authority config it references)
  --request-state     configs/arl2-request-state.json
                      (scripts/author-arl2-campaign-request.py; the driver
                      deployment pins the request file and both its shas)
  --release-root      the staged release checkout; every reviewed-source
                      pin and adapter code sha resolves inside it
  --database-url      the campaign database URL; its sha is pinned into
                      every database-holding config
  --capability-catalog / --resource-catalog
                      the frozen capability and resource catalog documents
                      staged by W1 step 4; their file shas feed the
                      compilation policy pin

Identity plan (runbook "Box layout"): one gid shared by the driver and
every service; uids - the driver and its four role subprocesses, one uid
for the eleven worker-facing services, one per kernel-command signer, one
for the executor, plus a dedicated uid for the worker role and the driver
uid for atomic-admission (a CAS writer).  The CAS root stays owned by the
driver identity (the authorities script created it that way), so
--driver-uid/--driver-gid MUST equal the CAS root's owner, and the gid
must be the driver identity's primary group: the writable-root custody
check pins st_uid==euid AND st_gid==egid (research_store/cas.py:94-101).

Custody topology (PI decision Q10(b), 2026-09-18): with the CAS root at
0750 the reader services (--service-uid) and the worker role
(--worker-uid) compose read-only archives through the group class at
uids DISTINCT from the owner; atomic-admission and the driver share the
owner uid as the only writers; the spool root follows the same mode
(0750, group-read for the driver, publication locks owner-only).  A 0700
root keeps the legacy shape where the worker role must ride the driver
uid and no distinct-uid reader can compose - the runbook's Q12 refusals.

Timestamps: one prepared_at T0 for the reader, every service pin, the
worker composition, every role deployment, and the driver config; pin
windows open five minutes before T0 and stay open --valid-hours;
campaign_deadline = T0 + --deadline-hours (absolute, and it must budget
every pause - runbook W4).  The driver invocation records sys.executable
(run this script under the control-plane python you want the driver to
use) and --driver-user names the unix identity the driver-control helper
starts the driver as.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORKER_SERVICES: tuple[str, ...] = (
    "action_proposal",
    "protocol_compilation",
    "execution_authorization",
    "execution_registration",
    "raw_run_source",
    "database_observation",
    "independent_validation",
    "committed_validation_source",
    "independent_admission",
    "atomic_admission",
    "continuation_assessment",
)
COMMAND_SERVICES: tuple[str, ...] = ("action_kernel_command", "transition_kernel_command")
CUPRATE_SERVICE = "cuprate_diagnostic"
ALL_SERVICES: tuple[str, ...] = (*WORKER_SERVICES, *COMMAND_SERVICES, CUPRATE_SERVICE)

# Live-keyed services whose config/deployment is authored at the first
# per-round pause, not here (see module docstring).
DEFERRED_SERVICES: tuple[str, ...] = (
    "protocol_compilation",
    "execution_authorization",
    "independent_validation",
)

# The per-service authority closure of the worker composition
# (worker_composition.py:431-450); each service's pin carries the sorted
# binding shas of exactly these roles.
_SERVICE_BINDING_ROLES: dict[str, tuple[str, ...]] = {
    "action_proposal": ("action_proposal",),
    "protocol_compilation": ("protocol_compilation",),
    "execution_authorization": ("execution_authorization",),
    "execution_registration": ("execution_authorization",),
    "raw_run_source": ("execution_authorization",),
    "database_observation": ("database_attestation",),
    "independent_validation": ("independent_validation",),
    "committed_validation_source": ("database_attestation", "independent_validation"),
    "independent_admission": ("independent_admission",),
    "atomic_admission": ("database_attestation", "independent_admission", "kernel_command"),
    "continuation_assessment": ("continuation_assessment",),
}

_SERVICE_FACTORIES: dict[str, tuple[str, str]] = {
    "action_proposal": (
        "aletheia.research_controller_action_proposal_runtime",
        "build_action_proposal_rpc_service",
    ),
    "execution_registration": (
        "aletheia.research_controller_execution_registration_runtime",
        "build_execution_registration_rpc_service",
    ),
    "raw_run_source": (
        "aletheia.research_controller_raw_run_source_runtime",
        "build_raw_run_source_rpc_service",
    ),
    "database_observation": (
        "aletheia.research_controller_database_observation_runtime",
        "build_database_observation_rpc_service",
    ),
    "committed_validation_source": (
        "aletheia.research_controller_committed_validation_runtime",
        "build_committed_validation_source_rpc_service",
    ),
    "independent_admission": (
        "aletheia.research_controller_independent_admission_runtime",
        "build_independent_admission_rpc_service",
    ),
    "atomic_admission": (
        "aletheia.research_controller_atomic_admission_runtime",
        "build_atomic_admission_rpc_service",
    ),
    "continuation_assessment": (
        "aletheia.research_controller_continuation_runtime",
        "build_continuation_assessment_rpc_service",
    ),
    "action_kernel_command": (
        "aletheia.research_controller_kernel_command_runtime",
        "build_action_kernel_command_rpc_service",
    ),
    "transition_kernel_command": (
        "aletheia.research_controller_kernel_command_runtime",
        "build_transition_kernel_command_rpc_service",
    ),
    CUPRATE_SERVICE: (
        "aletheia.research_controller_cuprate_runtime",
        "build_cuprate_diagnostic_rpc_service",
    ),
}

DRIVER_PRINCIPAL = "service.arl2.driver"
CONTROLLER_PRINCIPAL = "service.arl2.controller"
WORKER_PRINCIPAL = "service.arl2.worker"
ROLE_PRINCIPALS = {
    "kernel_dispatcher": "service.arl2.kernel-dispatcher",
    "terminal_dispatcher": "service.arl2.terminal-dispatcher",
    "worker": WORKER_PRINCIPAL,
    "delivery_reconciler": "service.arl2.delivery-reconciler",
}
ROLE_ORDER = ("kernel_dispatcher", "terminal_dispatcher", "worker", "delivery_reconciler")

# Scientific-bridge authority principals: the execution/validator/
# admission/database pins are shared across service configs, and the four
# primary-role services inherit them as their pin identities
# (worker_composition.py:466-476).
BRIDGE_PRINCIPALS = {
    "execution": "principal.arl2.bridge.execution-authorizer",
    "validator": "principal.arl2.bridge.observation-validator",
    "admission": "principal.arl2.bridge.observation-admitter",
    "database": "principal.arl2.bridge.observation-database",
}
SERVICE_PRINCIPALS = {
    "action_proposal": "service.arl2.action-proposal",
    "protocol_compilation": "service.arl2.protocol-compilation",
    "execution_registration": "service.arl2.execution-registration",
    "raw_run_source": "service.arl2.raw-run-source",
    "committed_validation_source": "service.arl2.committed-validation-source",
    "atomic_admission": "service.arl2.atomic-admission",
    "continuation_assessment": "service.arl2.continuation-assessor",
    "action_kernel_command": "service.arl2.action-kernel-command",
    "transition_kernel_command": "service.arl2.transition-kernel-command",
    CUPRATE_SERVICE: "service.arl2.cuprate-diagnostic",
}

QUALIFICATION_PRINCIPALS = {
    "pricing": "principal.qualification.pricing",
    "source_budget": "principal.qualification.source_budget",
    "qualification": "principal.qualification.qualification",
    "terminal_verification": "principal.qualification.terminal_verification",
    "runtime_control": "principal.qualification.runtime_control",
    "enrollment": "principal.qualification.enrollment",
    "transport": "principal.qualification.transport",
    "node": "node.arlcup-1",
    "allocator": "principal.qualification.allocator",
    "input_resolver": "principal.qualification.input_resolver",
    "artifact_verifier": "principal.qualification.artifact_verifier",
}

NODE_ID = "node.arlcup-1"
CURRENCY_CODE = "USD"
ARTIFACT_OBJECT_STORE_ID = "store:arl2-qualification"
MAX_OBJECT_BYTES = 64 * 1024 * 1024
ARTIFACT_MAX_OBJECT_BYTES = 1024**3

STATE_SCHEMA = "aletheia.arl2_deployment_state"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--working-root", required=True, help="window working root (e.g. /opt/aletheia/arl2-dryrun)")
    parser.add_argument("--release-root", required=True, help="staged release checkout (reviewed code root)")
    parser.add_argument("--database-url", required=True, help="campaign database URL")
    parser.add_argument("--activation-state", required=True, help="configs/arl2-activation-state.json path")
    parser.add_argument("--request-state", required=True, help="configs/arl2-request-state.json path")
    parser.add_argument("--capability-catalog", required=True, help="frozen capability catalog document (W1 step 4)")
    parser.add_argument("--resource-catalog", required=True, help="frozen resource catalog document (W1 step 4)")
    parser.add_argument("--driver-uid", type=int, required=True, help="driver identity uid (must own the CAS root)")
    parser.add_argument("--driver-gid", type=int, required=True, help="shared gid; the driver identity's primary group")
    parser.add_argument("--service-uid", type=int, required=True, help="uid for the worker-facing services (readers stay DISTINCT from the CAS owner uid)")
    parser.add_argument("--worker-uid", type=int, required=True, help="uid for the worker role subprocess (distinct from driver/services; primary gid = --driver-gid)")
    parser.add_argument("--admission-uid", type=int, required=True, help="uid for atomic-admission (a CAS writer: equals --driver-uid under the 0750 topology)")
    parser.add_argument("--action-signer-uid", type=int, required=True, help="uid for the action kernel-command signer")
    parser.add_argument("--transition-signer-uid", type=int, required=True, help="uid for the transition kernel-command signer")
    parser.add_argument("--cuprate-uid", type=int, required=True, help="uid for the cuprate diagnostic service / executor")
    parser.add_argument("--driver-user", default="arl2drv", help="unix user the driver-control helper starts (default arl2drv)")
    parser.add_argument("--valid-hours", type=float, default=96.0, help="pin validity window in hours (default 96)")
    parser.add_argument("--deadline-hours", type=float, default=24.0, help="campaign deadline hours after prepared_at (default 24)")
    parser.add_argument("--prepared-at", default=None, help="override prepared_at (ISO-8601 with offset); default now UTC")
    return parser


def _fail(message: str) -> None:
    raise SystemExit(f"author-arl2-deployments: {message}")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_canonical(path: Path, payload: bytes, *, mode: int, uid: int, gid: int) -> str:
    """Write bytes once with pinned custody; returns the file sha."""

    if path.exists():
        _fail(f"refusing to overwrite existing file {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(descriptor, payload)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)
    os.chown(path, uid, gid)
    return _sha256_bytes(payload)


def _mkdir_pinned(path: Path, *, mode: int, uid: int, gid: int) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def _stat_directory(path: Path) -> dict[str, int]:
    metadata = os.stat(path)
    return {
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "device_id": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _generate_ed25519(engine):
    """One raw Ed25519 keypair: (private_32b, public_hex)."""

    private = engine.generate_private_key(engine.Ed25519)
    return (
        private.private_bytes(
            engine.Encoding.Raw, engine.PrivateFormat.Raw, engine.NoEncryption()
        ),
        private.public_key()
        .public_bytes(engine.Encoding.Raw, engine.PublicFormat.Raw)
        .hex(),
    )


def _generate_x25519(engine):
    private = engine.generate_private_key(engine.X25519)
    return (
        private.private_bytes(
            engine.Encoding.Raw, engine.PrivateFormat.Raw, engine.NoEncryption()
        ),
        private.public_key()
        .public_bytes(engine.Encoding.Raw, engine.PublicFormat.Raw)
        .hex(),
    )


def _write_key(path: Path, material: bytes, *, uid: int, gid: int) -> str:
    return _write_canonical(path, material, mode=0o400, uid=uid, gid=gid)


def _read_bytes(path: Path) -> bytes:
    return Path(path).resolve(strict=True).read_bytes()


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> int:
    args = _parser().parse_args()

    if os.geteuid() != 0:
        _fail("must run as root (service custody is chowned to the service identities)")

    working = Path(args.working_root).resolve(strict=True)
    release = Path(args.release_root).resolve(strict=True)
    state_path = working / "configs" / "arl2-deployment-state.json"
    if state_path.exists():
        _fail(f"deployment state already exists at {state_path}; the window is write-once (abort doctrine)")

    # The release tree provides every model and derivation helper; the
    # database URL is pinned as an environment fact before any import
    # touches get_settings().
    os.environ["ALETHEIA_DATABASE_URL"] = args.database_url
    if str(release) not in sys.path:
        sys.path.insert(0, str(release))

    from aletheia.config import get_settings
    from aletheia.db import expected_schema_revision

    database_url_sha256 = _sha256_bytes(get_settings().database_url.encode("utf-8"))
    schema_revision = expected_schema_revision()

    from cryptography.hazmat.primitives import serialization as _serialization

    class _Engine:  # tiny indirection so the helpers above read clearly
        Encoding = _serialization.Encoding
        PrivateFormat = _serialization.PrivateFormat
        PublicFormat = _serialization.PublicFormat
        NoEncryption = _serialization.NoEncryption

        @staticmethod
        def generate_private_key(curve):
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            if curve is _Engine.Ed25519:
                return Ed25519PrivateKey.generate()
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

            return X25519PrivateKey.generate()

        Ed25519 = object()
        X25519 = object()

    engine = _Engine

    prepared_at = (
        datetime.fromisoformat(args.prepared_at) if args.prepared_at else datetime.now(timezone.utc)
    )
    if prepared_at.tzinfo is None:
        _fail("--prepared-at must carry an explicit UTC offset")
    prepared_at = prepared_at.astimezone(timezone.utc).replace(microsecond=0)
    valid_from = prepared_at - timedelta(minutes=5)
    expires_at = prepared_at + timedelta(hours=args.valid_hours)
    campaign_deadline = prepared_at + timedelta(hours=args.deadline_hours)

    activation = _load_activation_inputs(args.activation_state)
    request = _load_request_inputs(args.request_state)
    if activation["quest_id"] != request["quest_id"]:
        _fail("activation state and request state name different quests")
    if activation["database_url_sha256"] != database_url_sha256:
        _fail("activation authority was generated against a different database URL")

    cas_metadata = _stat_directory(activation["cas_root"])
    if cas_metadata["uid"] != args.driver_uid or cas_metadata["gid"] != args.driver_gid:
        _fail(
            "CAS root is owned by "
            f"{cas_metadata['uid']}:{cas_metadata['gid']} but the driver identity is "
            f"{args.driver_uid}:{args.driver_gid}; create the driver identity with the "
            "CAS root's gid as its primary group and re-run"
        )
    if cas_metadata["mode"] not in (0o700, 0o750):
        _fail(f"CAS root mode is {cas_metadata['mode']:#o}; the writer pin requires 0700 or 0750")
    # atomic-admission is a CAS writer: its runtime pins cas_owner_uid ==
    # process_uid, so it always rides the driver identity
    if args.admission_uid != args.driver_uid:
        _fail("atomic-admission must run at the driver uid (writable CAS custody)")
    if cas_metadata["mode"] == 0o750:
        # the read-only compose passes only through the group class at a
        # uid distinct from the owner: the worker role and the reader
        # services must both stay off the driver uid
        if args.worker_uid in (args.driver_uid, args.service_uid):
            _fail("the 0750 CAS topology requires a worker-role uid distinct from driver/services")
    elif args.worker_uid != args.driver_uid:
        # the 0700 root admits no read-only compose at any uid; the worker
        # role can only ride the driver identity there (legacy shape)
        _fail("the 0700 CAS topology cannot host a distinct worker-role uid; use 0750")

    service_uid = {
        **{name: args.service_uid for name in WORKER_SERVICES},
        "atomic_admission": args.admission_uid,
        "action_kernel_command": args.action_signer_uid,
        "transition_kernel_command": args.transition_signer_uid,
        CUPRATE_SERVICE: args.cuprate_uid,
    }

    layout = _build_layout(
        working,
        driver_uid=args.driver_uid,
        driver_gid=args.driver_gid,
        service_uid=service_uid,
        cas_directory_mode=cas_metadata["mode"],
    )

    keys = _generate_keys(
        layout,
        engine,
        activation=activation,
        service_uid=service_uid,
        driver_uid=args.driver_uid,
        driver_gid=args.driver_gid,
    )

    qualification = _build_qualification(
        working=working,
        layout=layout,
        engine=engine,
        activation=activation,
        keys=keys,
        prepared_at=prepared_at,
        valid_from=valid_from,
        expires_at=expires_at,
        service_uid=service_uid,
        driver_uid=args.driver_uid,
        driver_gid=args.driver_gid,
        release=release,
        resource_catalog=Path(args.resource_catalog).resolve(strict=True),
    )

    closure = _build_service_pins(
        layout,
        keys=keys,
        activation=activation,
        qualification=qualification,
        service_uid=service_uid,
        driver_uid=args.driver_uid,
        driver_gid=args.driver_gid,
        valid_from=valid_from,
        expires_at=expires_at,
        release=release,
        prepared_at=prepared_at,
        window_id=working.name,
        capability_catalog=Path(args.capability_catalog).resolve(strict=True),
        resource_catalog=Path(args.resource_catalog).resolve(strict=True),
    )

    controller_manifest, controller_paths = _build_controller_manifest(
        layout,
        release=release,
        prepared_at=prepared_at,
        window_id=working.name,
        worker_manifest_sha256=closure["identity"]["worker_manifest_sha256"],
        capability_catalog_sha256=closure["catalogs"]["capability"],
        driver_uid=args.driver_uid,
        driver_gid=args.driver_gid,
    )

    worker = _build_worker_composition(
        layout,
        release=release,
        controller_manifest=controller_manifest,
        controller_paths=controller_paths,
        closure=closure,
        qualification=qualification,
        activation=activation,
        cas_metadata=cas_metadata,
        prepared_at=prepared_at,
        database_url_sha256=database_url_sha256,
        schema_revision=schema_revision,
        driver_uid=args.driver_uid,
        driver_gid=args.driver_gid,
    )

    roles = _build_role_deployments(
        layout,
        release=release,
        controller_manifest=controller_manifest,
        controller_paths=controller_paths,
        qualification=qualification,
        worker=worker,
        prepared_at=prepared_at,
        database_url_sha256=database_url_sha256,
        schema_revision=schema_revision,
        driver_uid=args.driver_uid,
        driver_gid=args.driver_gid,
    )

    services = _build_service_deployments(
        layout,
        release=release,
        controller_manifest=controller_manifest,
        controller_paths=controller_paths,
        closure=closure,
        keys=keys,
        qualification=qualification,
        activation=activation,
        request=request,
        worker=worker,
        cas_metadata=cas_metadata,
        prepared_at=prepared_at,
        database_url_sha256=database_url_sha256,
        schema_revision=schema_revision,
        service_uid=service_uid,
        driver_uid=args.driver_uid,
        worker_uid=args.worker_uid,
        driver_gid=args.driver_gid,
    )

    driver_config, driver_deployment, driver_files, invocation = _build_driver(
        layout,
        release=release,
        activation=activation,
        request=request,
        controller_manifest=controller_manifest,
        controller_paths=controller_paths,
        roles=roles,
        closure=closure,
        prepared_at=prepared_at,
        campaign_deadline=campaign_deadline,
        database_url=args.database_url,
        database_url_sha256=database_url_sha256,
        schema_revision=schema_revision,
        driver_user=args.driver_user,
        driver_uid=args.driver_uid,
        worker_uid=args.worker_uid,
        driver_gid=args.driver_gid,
    )

    state_extra = _verify_offline(
        layout,
        services=services,
        roles=roles,
        driver_deployment=driver_files,
    )

    _write_state_file(
        state_path,
        args=args,
        working=working,
        prepared_at=prepared_at,
        valid_from=valid_from,
        expires_at=expires_at,
        campaign_deadline=campaign_deadline,
        activation=activation,
        request=request,
        cas_metadata=cas_metadata,
        database_url_sha256=database_url_sha256,
        schema_revision=schema_revision,
        controller_manifest=controller_manifest,
        controller_paths=controller_paths,
        closure=closure,
        qualification=qualification,
        keys=keys,
        service_uid=service_uid,
        worker=worker,
        roles=roles,
        services=services,
        driver_config=driver_config,
        driver_deployment=driver_deployment,
        driver_files=driver_files,
        invocation=invocation,
        state_extra=state_extra,
    )
    return 0


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------


def _service_dir_name(service: str) -> str:
    return service.replace("_", "-")


# Keyed by underscore service names (matching service_uid); the on-disk
# directory names below use the dash form via _service_dir_name.
_KEY_OWNERSHIP: dict[str, tuple[str, ...]] = {
    "execution_authorization": ("execution",),
    "database_observation": ("database",),
    "independent_validation": ("validator",),
    "independent_admission": ("admission",),
    "atomic_admission": ("database", "kernel"),
    "action_kernel_command": ("command",),
    "transition_kernel_command": ("command",),
}


def _build_layout(
    working: Path,
    *,
    driver_uid: int,
    driver_gid: int,
    service_uid: dict[str, int],
    cas_directory_mode: int,
) -> dict:
    """Create every custody directory once, with its final ownership."""

    layout: dict[str, object] = {"working": working}
    layout["configs"] = working / "configs"
    layout["service_configs"] = layout["configs"] / "services"
    layout["role_configs"] = layout["configs"] / "roles"
    layout["identity_manifests"] = layout["configs"] / "arl2-identity-manifests"
    layout["controller_policies"] = layout["configs"] / "controller-policies"
    layout["qualification_configs"] = layout["configs"] / "qualification"
    layout["qualification_policies"] = layout["qualification_configs"] / "policies"

    for directory in (
        layout["configs"],
        layout["service_configs"],
        layout["role_configs"],
        layout["identity_manifests"],
        layout["controller_policies"],
        layout["qualification_configs"],
        layout["qualification_policies"],
        working / "staging",
    ):
        _mkdir_pinned(directory, mode=0o755, uid=driver_uid, gid=driver_gid)

    # Sockets and receipt keys: one directory per service, so every
    # deployment's custody roots stay pairwise disjoint.
    sockets: dict[str, Path] = {}
    receipt_key_dirs: dict[str, Path] = {}
    domain_key_dirs: dict[str, dict[str, Path]] = {}
    every_uid = {**service_uid, "driver": driver_uid}
    for service, uid in every_uid.items():
        socket_dir = working / "sockets" / _service_dir_name(service)
        _mkdir_pinned(socket_dir, mode=0o750, uid=uid, gid=driver_gid)
        sockets[service] = socket_dir
        receipt_dir = working / "keys" / f"{_service_dir_name(service)}-receipt"
        _mkdir_pinned(receipt_dir, mode=0o700, uid=uid, gid=driver_gid)
        receipt_key_dirs[service] = receipt_dir
    for service, key_names in _KEY_OWNERSHIP.items():
        domain_key_dirs[service] = {}
        for key_name in key_names:
            key_dir = working / "keys" / f"{_service_dir_name(service)}-{key_name}"
            _mkdir_pinned(key_dir, mode=0o700, uid=every_uid[service], gid=driver_gid)
            domain_key_dirs[service][key_name] = key_dir
    layout["sockets"] = sockets
    layout["receipt_key_dirs"] = receipt_key_dirs
    layout["domain_key_dirs"] = domain_key_dirs

    # Qualification custody: the terminal reader's artifact store and the
    # frozen authority registry are disjoint roots; the reader is embedded
    # in eleven services and the terminal_dispatcher role, so the store is
    # service-owned but group-readable (0750) for the driver-side reader.
    qualification = working / "qualification"
    _mkdir_pinned(qualification, mode=0o755, uid=driver_uid, gid=driver_gid)
    layout["qualification_root"] = qualification
    layout["artifact_store_root"] = qualification / "artifact-store"
    layout["authority_registry_root"] = qualification / "authority-registry"

    # The F9-v2 validation archive: written by the F9 service, read by the
    # four sibling services at the same uid (0700 keeps it inside that
    # uid; the write config pins owner == process, the read configs pin
    # the same device+inode).
    layout["validation_archive_root"] = working / "f9-v2-validation-archive"
    _mkdir_pinned(
        layout["validation_archive_root"],
        mode=0o700,
        uid=service_uid["independent_validation"],
        gid=driver_gid,
    )

    # Continuation assessment artifacts: 0700 at the service uid (the
    # continuation runtime pins the root's owner == process).
    layout["continuation_artifact_root"] = working / "continuation-artifacts"
    _mkdir_pinned(
        layout["continuation_artifact_root"],
        mode=0o700,
        uid=service_uid["continuation_assessment"],
        gid=driver_gid,
    )

    # The action proposal spool: the mode follows the CAS topology. At 0750
    # the driver identity reads the published payloads through the group
    # class (Q11: the pin widens to 0750, group write stays denied, the
    # publication locks stay owner-only 0600); at 0700 the legacy
    # owner-private shape holds and the driver read stays contradictory.
    layout["action_proposal_spool_root"] = working / "spool" / "action-proposals"
    _mkdir_pinned(
        layout["action_proposal_spool_root"],
        mode=0o750 if cas_directory_mode == 0o750 else 0o700,
        uid=service_uid["action_proposal"],
        gid=driver_gid,
    )

    # Driver-side custody: bundle output and the qualification
    # commissioning keys live under the working root, disjoint from the
    # CAS root (which the authorities script created outside it).
    layout["bundle_output_root"] = working / "bundle"
    _mkdir_pinned(layout["bundle_output_root"], mode=0o755, uid=driver_uid, gid=driver_gid)
    layout["qualification_key_root"] = working / "keys" / "qualification"
    _mkdir_pinned(layout["qualification_key_root"], mode=0o700, uid=driver_uid, gid=driver_gid)

    return layout


# --------------------------------------------------------------------------
# Input state loaders
# --------------------------------------------------------------------------


def _load_activation_inputs(state_path: str) -> dict:
    """Parse the activation state/authority pair into models and facts."""

    from aletheia.research_kernel.policy import (
        ResearchAuthorizationPolicyV1,
        ResearchAuthorizationTrustRootV1,
    )
    from aletheia.research_kernel.schemas import canonical_sha256

    state = json.loads(_read_bytes(Path(state_path)))
    if state.get("schema_name") != "aletheia.arl2_activation_state":
        _fail(f"{state_path} is not an ARL-2 activation state file")
    authority_path = Path(state["authority_manifest_path"])
    authority = json.loads(_read_bytes(authority_path))
    if authority.get("schema_name") != "aletheia.arl2_activation_authority":
        _fail(f"{authority_path} is not an ARL-2 activation authority file")

    trust_root = ResearchAuthorizationTrustRootV1.model_validate(authority["trust_root"])
    policy = ResearchAuthorizationPolicyV1.model_validate(authority["policy"])
    if canonical_sha256(trust_root) != state["trust_root_sha256"]:
        _fail("activation trust root does not hash to the state's pin")
    if canonical_sha256(policy) != state["policy_sha256"]:
        _fail("activation policy does not hash to the state's pin")

    ordinary = authority["role_keys"]["ordinary"]
    ordinary_bytes = _read_bytes(Path(ordinary["path"]))
    if _sha256_bytes(bytes.fromhex(ordinary["public_key_ed25519_hex"])) != ordinary["key_id"]:
        _fail("activation ordinary key id does not match its recorded public key")

    valid_from = datetime.fromisoformat(authority["valid_from"])
    expires_at = datetime.fromisoformat(authority["expires_at"])
    if valid_from.tzinfo is None or expires_at.tzinfo is None:
        _fail("activation authority windows must carry explicit offsets")

    return {
        "quest_id": state["quest_id"],
        "root_branch_id": state["root_branch_id"],
        "trust_root": trust_root,
        "policy": policy,
        "policy_sha256": state["policy_sha256"],
        "cas_root": Path(authority["cas_root"]),
        "command_files": state["command_files"],
        "ordinary": {
            "path": ordinary["path"],
            "key_id": ordinary["key_id"],
            "public_key_ed25519_hex": ordinary["public_key_ed25519_hex"],
            "principal_id": ordinary["principal_id"],
            "bytes": ordinary_bytes,
        },
        "auditor": authority["auxiliary_keys"]["auditor"],
        "qualifier": authority["auxiliary_keys"]["qualifier"],
        "valid_from": valid_from,
        "expires_at": expires_at,
        "database_url_sha256": authority["database_url_sha256"],
        "stream_version_after_activation": state["stream_version_after_activation"],
    }


def _load_request_inputs(state_path: str) -> dict:
    """Parse the request state and recompute both request shas."""

    from aletheia.arl2_runtime import ARL2QuestionCampaignRequestV1
    from aletheia.research_kernel.schemas import canonical_json_bytes

    state = json.loads(_read_bytes(Path(state_path)))
    if state.get("schema_name") != "aletheia.arl2_request_state":
        _fail(f"{state_path} is not an ARL-2 request state file")

    request_path = Path(state["request_path"])
    request_bytes = _read_bytes(request_path)
    request = ARL2QuestionCampaignRequestV1.model_validate_json(request_bytes)
    if canonical_json_bytes(request) != request_bytes:
        _fail("request file bytes are not the canonical model encoding")
    if request.request_sha256 != state["request_sha256"]:
        _fail("request state pins a different request sha than the request file")
    if request.request_id != f"arl2q_{request.request_sha256[:32]}":
        _fail("request id does not derive from the request sha")

    dataset_path = Path(state["dataset_content"]["path"])
    try:
        dataset_info = dataset_path.lstat()
    except OSError as exc:
        _fail(f"cannot stat the staged dataset CSV {dataset_path}: {exc}")
    if (
        dataset_path.is_symlink()
        or not stat.S_ISREG(dataset_info.st_mode)
        or stat.S_IMODE(dataset_info.st_mode) & 0o222
        or stat.S_IMODE(dataset_info.st_mode) & 0o044 == 0
    ):
        _fail(
            f"staged dataset CSV {dataset_path} has unsafe custody for the cuprate "
            "service (regular file, no write bits, readable beyond the owner uid; "
            "stage it 0444)"
        )

    return {
        "quest_id": state["quest_id"],
        "request_path": str(request_path),
        "request_file_sha256": _sha256_bytes(request_bytes),
        "request_sha256": request.request_sha256,
        "request_id": request.request_id,
        "dataset_csv_path": state["dataset_content"]["path"],
        "dataset_content_sha256": state["dataset_content"]["file_sha256"],
    }


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------


def _generate_keys(layout, engine, *, activation, service_uid, driver_uid, driver_gid) -> dict:
    """Generate every transport receipt key, domain key, and qualification key."""

    from aletheia.research_controller.external_rpc import controller_worker_rpc_key_id
    from aletheia.observations.scientific_bridge import scientific_bridge_key_id

    keys: dict[str, dict] = {"receipt": {}, "domain": {}, "qualification": {}}

    for service in ALL_SERVICES:
        private, public_hex = _generate_ed25519(engine)
        path = layout["receipt_key_dirs"][service] / "receipt.key"
        keys["receipt"][service] = {
            "path": str(path),
            "private": private,
            "public_hex": public_hex,
            "key_id": controller_worker_rpc_key_id(public_hex),
            "file_sha256": _write_key(path, private, uid=service_uid[service], gid=driver_gid),
        }

    def domain_key(service: str, key_name: str, material: bytes, public_hex: str) -> dict:
        path = layout["domain_key_dirs"][service][key_name] / f"{key_name}.key"
        return {
            "path": str(path),
            "private": material,
            "public_hex": public_hex,
            "key_id": scientific_bridge_key_id(public_hex),
            "file_sha256": _write_key(path, material, uid=service_uid[service], gid=driver_gid),
        }

    def fresh_domain(service: str, key_name: str) -> dict:
        material, public_hex = _generate_ed25519(engine)
        return domain_key(service, key_name, material, public_hex)

    # One bridge key per scientific authority; the AA service gets its own
    # custody copies of the database key (same authority) and the
    # activation ORDINARY kernel key (it commits kernel events as the
    # kernel authority), so every deployment's key paths stay disjoint.
    keys["domain"]["execution"] = fresh_domain("execution_authorization", "execution")
    keys["domain"]["database"] = fresh_domain("database_observation", "database")
    keys["domain"]["validator"] = fresh_domain("independent_validation", "validator")
    keys["domain"]["admission"] = fresh_domain("independent_admission", "admission")
    keys["domain"]["atomic_database"] = domain_key(
        "atomic_admission",
        "database",
        keys["domain"]["database"]["private"],
        keys["domain"]["database"]["public_hex"],
    )
    ordinary = activation["ordinary"]
    keys["domain"]["atomic_kernel"] = {
        "path": str(layout["domain_key_dirs"]["atomic_admission"]["kernel"] / "kernel.key"),
        "private": ordinary["bytes"],
        "public_hex": ordinary["public_key_ed25519_hex"],
        "key_id": ordinary["key_id"],
        "file_sha256": _write_key(
            layout["domain_key_dirs"]["atomic_admission"]["kernel"] / "kernel.key",
            ordinary["bytes"],
            uid=service_uid["atomic_admission"],
            gid=driver_gid,
        ),
    }
    for service in COMMAND_SERVICES:
        keys["domain"][service] = {
            "path": str(layout["domain_key_dirs"][service]["command"] / "command.key"),
            "private": ordinary["bytes"],
            "public_hex": ordinary["public_key_ed25519_hex"],
            "key_id": ordinary["key_id"],
            "file_sha256": _write_key(
                layout["domain_key_dirs"][service]["command"] / "command.key",
                ordinary["bytes"],
                uid=service_uid[service],
                gid=driver_gid,
            ),
        }

    # Qualification commissioning keys (driver custody).  The
    # qualification/terminal-verification authorities reuse the
    # activation qualifier/auditor PUBLIC keys; everything else is fresh.
    def qualification_key(name: str) -> dict:
        material, public_hex = _generate_ed25519(engine)
        path = layout["qualification_key_root"] / f"{name}.key"
        return {
            "path": str(path),
            "private": material,
            "public_hex": public_hex,
            "key_id": _sha256_bytes(bytes.fromhex(public_hex)),
            "file_sha256": _write_key(path, material, uid=driver_uid, gid=driver_gid),
        }

    for name in ("pricing", "source_budget", "runtime_control", "enrollment"):
        keys["qualification"][name] = qualification_key(name)

    signing_material, signing_public = _generate_ed25519(engine)
    keys["qualification"]["node_signing"] = {
        "path": str(layout["qualification_key_root"] / "node-signing.key"),
        "private": signing_material,
        "public_hex": signing_public,
        "key_id": _sha256_bytes(bytes.fromhex(signing_public)),
        "file_sha256": _write_key(
            layout["qualification_key_root"] / "node-signing.key",
            signing_material,
            uid=driver_uid,
            gid=driver_gid,
        ),
    }
    transport_material, transport_public = _generate_x25519(engine)
    keys["qualification"]["node_transport"] = {
        "private": transport_material,
        "public_hex": transport_public,
    }

    if keys["domain"]["atomic_database"]["file_sha256"] == keys["receipt"]["atomic_admission"]["file_sha256"]:
        _fail("atomic admission database key collides with its transport receipt key")
    return keys


# --------------------------------------------------------------------------
# Qualification custody (reader R, custody C, node, registry, artifact store)
# --------------------------------------------------------------------------


def _policy_note(window_id: str, subject: str, prepared_at: datetime, policy: str) -> dict:
    return {
        "schema_name": "aletheia.arl2_policy_note",
        "schema_version": 1,
        "window_id": window_id,
        "subject": subject,
        "policy": policy,
        "prepared_at": _iso(prepared_at),
    }


def _build_qualification(
    *,
    working,
    layout,
    engine,
    activation,
    keys,
    prepared_at,
    valid_from,
    expires_at,
    service_uid,
    driver_uid,
    driver_gid,
    release,
    resource_catalog,
) -> dict:
    """Build R, C, the node authority, the frozen registry, and the artifact store."""

    from aletheia.execution.artifact_store import LocalArtifactStore
    from aletheia.execution.assignment_contracts import NodeAssignmentTransportPin, node_transport_key_id
    from aletheia.execution.authority_contracts import (
        AuthorityRegistryFilesystemPin,
        ExecutionRateCard,
        ExecutionRateCardLine,
        PricingAuthorityPin,
        SourceBudgetAuthorityPin,
        detached_signature_message,
    )
    from aletheia.execution.authority_contracts import PRICING_RATE_CARD_SIGNATURE_DOMAIN
    from aletheia.execution.qualification_custody import QualificationPreAdmissionCustodyConfig
    from aletheia.execution.runtime_contracts import (
        NodeEnrollmentAuthorityPin,
        QualificationAuthorityPin,
        TerminalVerificationAuthorityPin,
        WorkerNodeManifest,
        issue_worker_node_enrollment,
    )
    from aletheia.execution.runtime_v2_contracts import RuntimeControlAuthorityPin
    from aletheia.execution.schemas import NetworkPolicy, StaticResourceCatalog
    from aletheia.execution.terminal_runtime import (
        QualificationTerminalReaderConfig,
        TerminalNodeAuthorityConfig,
    )
    from aletheia.observations.scientific_bridge import (
        VerifiedExecutionAuthorityProjection,
    )
    from aletheia.research_kernel.schemas import canonical_json_bytes
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    window_id = Path(working).name
    policies: dict[str, str] = {}

    # The executor-side resource namespace is the catalog's DERIVED class id
    # (rsc_<sha-prefix>): ExecutionResourceRequest accepts only those
    # (execution/schemas.py _RESOURCE_CLASS_ID_PATTERN), and the SEA rate-card
    # match requires exact tuple equality with the work-order node's request,
    # so a symbolic class key here would wedge the window at the first pause.
    catalog_classes = StaticResourceCatalog.model_validate(
        json.loads(_read_bytes(resource_catalog))
    ).resource_classes
    if len(catalog_classes) != 1:
        _fail(
            "the pinned resource catalog must carry exactly one class for this "
            f"window; found {len(catalog_classes)}"
        )
    resource_class_id = catalog_classes[0].resource_class_id

    def write_policy(name: str, policy: str) -> str:
        path = layout["qualification_policies"] / f"{name}.json"
        payload = canonical_json_bytes(_policy_note(window_id, name, prepared_at, policy))
        policies[name] = _write_canonical(path, payload, mode=0o644, uid=driver_uid, gid=driver_gid)
        return policies[name]

    for name, policy in (
        ("pricing", "Rate cards for the ARL-2 dry-run window; pricing authority signs execution rate cards only."),
        ("source-budget", "Source budget authority for the ARL-2 dry-run window."),
        ("runtime-control", "Runtime control authority for the ARL-2 dry-run window; qualification-only."),
        ("enrollment", "Worker node enrollment authority for the ARL-2 dry-run window."),
        ("transport", "Node assignment transport authority for the ARL-2 dry-run window."),
        ("node-sandbox", "Executor sandbox policy: staged dataset, no egress, deterministic compute."),
        ("node-egress", "Executor egress policy: none."),
        ("qualification", "Quest qualification authority (activation qualifier key)."),
        ("terminal-verification", "Terminal verification authority (activation auditor key)."),
        ("artifact-verification", "Artifact verification projection for raw-run custody."),
        ("cost-screening", "Deterministic proposal cost screening; screening is never authorization."),
        ("risk-screening", "Deterministic proposal risk screening; screening is never authorization."),
    ):
        write_policy(name, policy)

    # ---- authority pins -------------------------------------------------
    # The qualification and terminal-verification authorities reuse the
    # activation qualifier/auditor PUBLIC keys (their key ids are checked
    # against the public key bytes before use); the other four are fresh
    # commissioning keys generated above.
    def _aux_entry(name: str) -> tuple[str, str]:
        entry = activation[name]
        public_hex = entry["public_key_ed25519_hex"]
        if entry["key_id"] != _sha256_bytes(bytes.fromhex(public_hex)):
            _fail(f"activation {name} auxiliary key id does not match its public key")
        return entry["key_id"], public_hex

    qualification_key_id, qualification_public_hex = _aux_entry("qualifier")
    terminal_key_id, terminal_public_hex = _aux_entry("auditor")

    def authority_pin(model, *, policy_sha256: str, principal_id: str, key_id: str, public_hex: str):
        return model(
            policy_sha256=policy_sha256,
            principal_id=principal_id,
            key_id=key_id,
            public_key_ed25519_hex=public_hex,
            valid_from=valid_from,
            expires_at=expires_at,
        )

    pricing_pin = authority_pin(
        PricingAuthorityPin,
        policy_sha256=policies["pricing"],
        principal_id=QUALIFICATION_PRINCIPALS["pricing"],
        key_id=keys["qualification"]["pricing"]["key_id"],
        public_hex=keys["qualification"]["pricing"]["public_hex"],
    )
    budget_pin = authority_pin(
        SourceBudgetAuthorityPin,
        policy_sha256=policies["source-budget"],
        principal_id=QUALIFICATION_PRINCIPALS["source_budget"],
        key_id=keys["qualification"]["source_budget"]["key_id"],
        public_hex=keys["qualification"]["source_budget"]["public_hex"],
    )
    qualification_pin = authority_pin(
        QualificationAuthorityPin,
        policy_sha256=policies["qualification"],
        principal_id=QUALIFICATION_PRINCIPALS["qualification"],
        key_id=qualification_key_id,
        public_hex=qualification_public_hex,
    )
    terminal_pin = authority_pin(
        TerminalVerificationAuthorityPin,
        policy_sha256=policies["terminal-verification"],
        principal_id=QUALIFICATION_PRINCIPALS["terminal_verification"],
        key_id=terminal_key_id,
        public_hex=terminal_public_hex,
    )
    runtime_pin = authority_pin(
        RuntimeControlAuthorityPin,
        policy_sha256=policies["runtime-control"],
        principal_id=QUALIFICATION_PRINCIPALS["runtime_control"],
        key_id=keys["qualification"]["runtime_control"]["key_id"],
        public_hex=keys["qualification"]["runtime_control"]["public_hex"],
    )
    enrollment_pin = authority_pin(
        NodeEnrollmentAuthorityPin,
        policy_sha256=policies["enrollment"],
        principal_id=QUALIFICATION_PRINCIPALS["enrollment"],
        key_id=keys["qualification"]["enrollment"]["key_id"],
        public_hex=keys["qualification"]["enrollment"]["public_hex"],
    )

    # ---- worker node authority ------------------------------------------
    agent_source = release / "aletheia" / "execution" / "cuprate" / "service.py"
    manifest = WorkerNodeManifest(
        node_id=NODE_ID,
        site_id="site.arl2-dryrun",
        principal_id=QUALIFICATION_PRINCIPALS["node"],
        agent_version="arl2-cuprate-env-1",
        agent_implementation_sha256=_sha256_bytes(_read_bytes(agent_source)),
        operating_system="linux",
        cpu_architecture="x86_64",
        oci_platform="linux/amd64",
        container_runtime="host",
        sandbox_policy_sha256=policies["node-sandbox"],
        resource_class_ids=(resource_class_id,),
        allowed_data_classifications=("public",),
        network_policies=(NetworkPolicy.ALLOWLIST,),
        egress_policy_sha256=policies["node-egress"],
        node_signing_key_id=keys["qualification"]["node_signing"]["key_id"],
        node_signing_public_key_ed25519_hex=keys["qualification"]["node_signing"]["public_hex"],
        key_valid_from=valid_from,
        key_expires_at=expires_at,
        key_revoked_at=None,
        frozen_at=prepared_at,
    )
    enrollment = issue_worker_node_enrollment(
        manifest=manifest,
        pin=enrollment_pin,
        private_key=keys["qualification"]["enrollment"]["private"],
        issued_at=prepared_at,
        expires_at=expires_at,
    )
    transport_pin = NodeAssignmentTransportPin(
        node_id=NODE_ID,
        node_manifest_sha256=manifest.manifest_sha256,
        transport_policy_sha256=policies["transport"],
        transport_principal_id=QUALIFICATION_PRINCIPALS["transport"],
        transport_key_id=node_transport_key_id(keys["qualification"]["node_transport"]["public_hex"]),
        public_key_x25519_hex=keys["qualification"]["node_transport"]["public_hex"],
        valid_from=valid_from,
        expires_at=expires_at,
    )
    node_authority = TerminalNodeAuthorityConfig(
        manifest=manifest,
        enrollment=enrollment,
        enrollment_authority_pin=enrollment_pin,
        assignment_transport_pin=transport_pin,
    )

    # ---- frozen authority registry (one zero-cost rate card) -------------
    card = ExecutionRateCard(
        pricing_policy_sha256=pricing_pin.policy_sha256,
        issued_by_principal_id=pricing_pin.principal_id,
        pricing_authority_key_id=pricing_pin.key_id,
        valid_from=valid_from,
        expires_at=expires_at,
        revoked_at=None,
        lines=(
            ExecutionRateCardLine(
                accepted_resource_class_ids=(resource_class_id,),
                currency_code=CURRENCY_CODE,
                fixed_charge_microunits=0,
                charge_per_second_microunits=0,
                maximum_lease_seconds=3600,
            ),
        ),
    )
    card_payload = canonical_json_bytes(card)
    card_sha = _sha256_bytes(card_payload)
    card_signature = Ed25519PrivateKey.from_private_bytes(
        keys["qualification"]["pricing"]["private"]
    ).sign(
        detached_signature_message(
            signature_domain=PRICING_RATE_CARD_SIGNATURE_DOMAIN,
            canonical_payload=card_payload,
        )
    )

    registry_root = layout["authority_registry_root"]
    for namespace in (
        "rate_cards",
        "execution_cost_quotes",
        "source_budgets",
        "source_budget_projections",
    ):
        (registry_root / namespace / "sha256").mkdir(parents=True, exist_ok=True)
    card_dir = registry_root / "rate_cards" / "sha256" / card_sha[:2]
    card_dir.mkdir(parents=True, exist_ok=True)
    _write_canonical(card_dir / f"{card_sha}.json", card_payload, mode=0o444, uid=driver_uid, gid=driver_gid)
    _write_canonical(card_dir / f"{card_sha}.sig", card_signature, mode=0o444, uid=driver_uid, gid=driver_gid)
    # rglob never yields the root itself, and the root was first created by
    # this root-run script: freeze the root alongside its children or the
    # registry pin (owner, 0o555) will not match the root's real custody.
    for entry in (registry_root, *sorted(registry_root.rglob("*"))):
        os.chown(entry, driver_uid, driver_gid)
        if entry.is_dir():
            os.chmod(entry, 0o555)
        else:
            os.chmod(entry, 0o444)
    registry_metadata = _stat_directory(registry_root)
    registry_pin = AuthorityRegistryFilesystemPin(
        registry_id="registry:arl2-dryrun-authorities",
        owner_uid=registry_metadata["uid"],
        device_id=registry_metadata["device_id"],
        directory_mode=0o555,
        file_mode=0o444,
    )

    # ---- artifact store ---------------------------------------------------
    artifact_root = layout["artifact_store_root"]
    store = LocalArtifactStore(
        artifact_root,
        verifier_principal_id=QUALIFICATION_PRINCIPALS["artifact_verifier"],
        object_store_id=ARTIFACT_OBJECT_STORE_ID,
        max_object_bytes=ARTIFACT_MAX_OBJECT_BYTES,
    )
    del store
    # Every worker-facing service shares one uid (arl2svc); any entry of the
    # map names it. The writable ctor just created the root and six child
    # trees as THIS root-run process: hand the whole subtree over (the
    # read-only ctor opens each child, so root-owned 0o700 children would
    # EACCES every service). The store is empty at authoring time; files
    # written later carry their own custody.
    for entry in (artifact_root, *sorted(artifact_root.rglob("*"))):
        os.chown(entry, service_uid["action_proposal"], driver_gid)
        if entry.is_dir():
            os.chmod(entry, 0o750)

    # ---- reader R and pre-admission custody C -----------------------------
    reader = QualificationTerminalReaderConfig(
        artifact_store_root=str(artifact_root),
        artifact_verifier_principal_id=QUALIFICATION_PRINCIPALS["artifact_verifier"],
        artifact_object_store_id=ARTIFACT_OBJECT_STORE_ID,
        artifact_max_object_bytes=ARTIFACT_MAX_OBJECT_BYTES,
        authority_registry_root=str(registry_root),
        authority_registry_filesystem_pin=registry_pin,
        pricing_authority_pin=pricing_pin,
        source_budget_authority_pin=budget_pin,
        qualification_authority_pin=qualification_pin,
        terminal_verification_authority_pin=terminal_pin,
        runtime_control_authority_pin=runtime_pin,
        node_authorities=(node_authority,),
        allowed_rate_card_sha256s=(card_sha,),
        allowed_currency_codes=(CURRENCY_CODE,),
        allocator_principal_id=QUALIFICATION_PRINCIPALS["allocator"],
        input_resolver_principal_id=QUALIFICATION_PRINCIPALS["input_resolver"],
        prepared_at=prepared_at,
    )
    custody = QualificationPreAdmissionCustodyConfig(
        artifact_store_root=reader.artifact_store_root,
        artifact_verifier_principal_id=reader.artifact_verifier_principal_id,
        artifact_object_store_id=reader.artifact_object_store_id,
        artifact_max_object_bytes=reader.artifact_max_object_bytes,
        authority_registry_root=reader.authority_registry_root,
        authority_registry_filesystem_pin=reader.authority_registry_filesystem_pin,
        pricing_authority_pin=pricing_pin,
        source_budget_authority_pin=budget_pin,
        qualification_authority_pin=qualification_pin,
        terminal_verification_authority_pin=terminal_pin,
        input_resolver_principal_id=reader.input_resolver_principal_id,
        prepared_at=prepared_at,
    )

    # The raw-run custody projection of the artifact verifier: an opaque
    # pin derived from the real artifact-verification policy document.
    artifact_authority = VerifiedExecutionAuthorityProjection(
        principal_id=QUALIFICATION_PRINCIPALS["artifact_verifier"],
        key_id=policies["artifact-verification"],
        policy_sha256=policies["artifact-verification"],
    )

    return {
        "reader": reader,
        "reader_json": reader.model_dump(mode="json"),
        "custody": custody,
        "custody_json": custody.model_dump(mode="json"),
        "runtime_pin": runtime_pin,
        "node_authority": node_authority,
        "policies": policies,
        "rate_card_sha256": card_sha,
        "artifact_authority": artifact_authority,
        "artifact_authority_json": artifact_authority.model_dump(mode="json"),
    }


# --------------------------------------------------------------------------
# Service pins, policies, identity manifests, authority bindings
# --------------------------------------------------------------------------

def _build_service_pins(
    layout,
    *,
    keys,
    activation,
    qualification,
    service_uid,
    driver_uid,
    driver_gid,
    valid_from,
    expires_at,
    release,
    prepared_at,
    window_id,
    capability_catalog,
    resource_catalog,
) -> dict:
    """Author policies, identity manifests, bindings, and all fourteen pins."""

    from aletheia.observations.scientific_bridge import (
        ObservationDatabaseAuthorityPin,
        ScientificBridgeAuthorityPin,
        ScientificBridgeRole,
    )
    from aletheia.protocols.schemas import ProtocolActionCategory
    from aletheia.research_controller.action_proposal_provider import (
        DeterministicActionProposalPolicyPin,
    )
    from aletheia.research_controller.continuation import OBSERVED_OUTCOME_IDENTITY_POLICY_SHA256
    from aletheia.research_controller.continuation_assessor import EXACT_OUTCOME_BIN_FIT_RULE_SHA256
    from aletheia.research_controller.continuation_step import ContinuationAssessmentPolicyPin
    from aletheia.research_controller.external_rpc import (
        ControllerWorkerRPCOperation,
        ControllerWorkerRPCServicePin,
    )
    from aletheia.research_controller.protocol_compilation_step import (
        ActionProtocolCategoryPolicy,
        ProtocolCompilationPolicyPin,
    )
    from aletheia.research_controller.step_executor import (
        ControllerStepAuthorityBinding,
        ControllerStepAuthorityRole,
    )
    from aletheia.research_kernel.schemas import ActionKind, canonical_json_bytes

    # Operation sets mirror worker_composition._SERVICE_OPERATIONS, but the
    # pin validator (external_rpc.py:182) demands enum-VALUE-sorted tuples
    # (worker_composition builds them through its _operations() sort), so
    # the multi-op entries below are in sorted order, not workflow order.
    operations = {
        "action_proposal": (ControllerWorkerRPCOperation.MATERIALIZE_ACTION_PROPOSAL,),
        "protocol_compilation": (ControllerWorkerRPCOperation.COMPILE_PROTOCOL,),
        "execution_authorization": (ControllerWorkerRPCOperation.ISSUE_EXECUTION_AUTHORIZATION,),
        "execution_registration": (
            ControllerWorkerRPCOperation.REGISTER_EXECUTION,
            ControllerWorkerRPCOperation.REGISTER_EXECUTION_CAMPAIGN,
        ),
        "raw_run_source": (ControllerWorkerRPCOperation.LOAD_RAW_RUN,),
        "database_observation": (
            ControllerWorkerRPCOperation.COMMIT_VALIDATION,
            ControllerWorkerRPCOperation.ISSUE_ADMISSION_CHALLENGE,
            ControllerWorkerRPCOperation.ISSUE_VALIDATION_CHALLENGE,
        ),
        "independent_validation": (
            ControllerWorkerRPCOperation.ISSUE_VALIDATION_RECEIPT,
            ControllerWorkerRPCOperation.PREPARE_VALIDATION_CAMPAIGN,
        ),
        "committed_validation_source": (ControllerWorkerRPCOperation.LOAD_COMMITTED_VALIDATION,),
        "independent_admission": (ControllerWorkerRPCOperation.ISSUE_ADMISSION_DECISION,),
        "atomic_admission": (ControllerWorkerRPCOperation.COMMIT_AND_INCORPORATE,),
        "continuation_assessment": (ControllerWorkerRPCOperation.DERIVE_CONTINUATION,),
        "action_kernel_command": (ControllerWorkerRPCOperation.SIGN_ACTION_COMMAND,),
        "transition_kernel_command": (ControllerWorkerRPCOperation.SIGN_TRANSITION_COMMAND,),
        CUPRATE_SERVICE: (ControllerWorkerRPCOperation.RUN_CUPRATE_DIAGNOSTIC,),
    }

    max_bytes = {
        "action_proposal": 8 * 1024 * 1024,
        "protocol_compilation": 8 * 1024 * 1024,
        "execution_authorization": 8 * 1024 * 1024,
        "execution_registration": 8 * 1024 * 1024,
        "raw_run_source": 8 * 1024 * 1024,
        "database_observation": 16 * 1024 * 1024,
        "independent_validation": 16 * 1024 * 1024,
        "committed_validation_source": 8 * 1024 * 1024,
        "independent_admission": 8 * 1024 * 1024,
        "atomic_admission": 16 * 1024 * 1024,
        "continuation_assessment": 1024 * 1024,
        "action_kernel_command": 4 * 1024 * 1024,
        "transition_kernel_command": 4 * 1024 * 1024,
        CUPRATE_SERVICE: 8 * 1024 * 1024,
    }

    bridge_policy_root = layout["configs"] / "bridge-policies"
    service_policy_root = layout["configs"] / "service-policies"
    for directory in (bridge_policy_root, service_policy_root):
        _mkdir_pinned(directory, mode=0o755, uid=driver_uid, gid=driver_gid)

    def policy_doc(root: Path, name: str, subject: str, policy: str) -> str:
        path = root / f"{name}.json"
        payload = canonical_json_bytes(_policy_note(window_id, subject, prepared_at, policy))
        return _write_canonical(path, payload, mode=0o644, uid=driver_uid, gid=driver_gid)

    # ---- scientific-bridge authority pins ---------------------------------
    bridge_policies = {
        name: policy_doc(
            bridge_policy_root,
            f"bridge-{name}",
            f"bridge:{name}",
            text,
        )
        for name, text in (
            ("execution", "Scientific execution authorization for the ARL-2 dry-run window."),
            ("validator", "Independent observation validation for the ARL-2 dry-run window."),
            ("admission", "Independent observation admission for the ARL-2 dry-run window."),
            ("database", "Observation database attestation for the ARL-2 dry-run window."),
        )
    }
    bridge = {
        "execution": ScientificBridgeAuthorityPin(
            role=ScientificBridgeRole.EXECUTION_AUTHORIZER,
            policy_sha256=bridge_policies["execution"],
            principal_id=BRIDGE_PRINCIPALS["execution"],
            key_id=keys["domain"]["execution"]["key_id"],
            public_key_ed25519_hex=keys["domain"]["execution"]["public_hex"],
            valid_from=valid_from,
            expires_at=expires_at,
        ),
        "validator": ScientificBridgeAuthorityPin(
            role=ScientificBridgeRole.OBSERVATION_VALIDATOR,
            policy_sha256=bridge_policies["validator"],
            principal_id=BRIDGE_PRINCIPALS["validator"],
            key_id=keys["domain"]["validator"]["key_id"],
            public_key_ed25519_hex=keys["domain"]["validator"]["public_hex"],
            valid_from=valid_from,
            expires_at=expires_at,
        ),
        "admission": ScientificBridgeAuthorityPin(
            role=ScientificBridgeRole.OBSERVATION_ADMITTER,
            policy_sha256=bridge_policies["admission"],
            principal_id=BRIDGE_PRINCIPALS["admission"],
            key_id=keys["domain"]["admission"]["key_id"],
            public_key_ed25519_hex=keys["domain"]["admission"]["public_hex"],
            valid_from=valid_from,
            expires_at=expires_at,
        ),
        "database": ObservationDatabaseAuthorityPin(
            policy_sha256=bridge_policies["database"],
            principal_id=BRIDGE_PRINCIPALS["database"],
            key_id=keys["domain"]["database"]["key_id"],
            public_key_ed25519_hex=keys["domain"]["database"]["public_hex"],
            valid_from=valid_from,
            expires_at=expires_at,
        ),
    }

    # ---- the three keyless step policy pins --------------------------------
    proposal_policy = DeterministicActionProposalPolicyPin(
        provider_implementation_sha256=_sha256_bytes(
            _read_bytes(release / "aletheia" / "research_controller" / "action_proposal_provider.py")
        ),
        provider_principal_id=SERVICE_PRINCIPALS["action_proposal"],
        initial_action_kind_preference=tuple(
            kind for kind in ActionKind if kind is not ActionKind.ACTIVATE
        ),
        initial_epistemic_purpose=(
            "Propose the discriminating diagnostic action that maximizes information "
            "about the active hypothesis under the frozen protocol catalog."
        ),
        redesign_epistemic_purpose=(
            "Propose a redesigned observable after a REFINE_COMMITTED round, keeping "
            "the quest's measurement identity chain intact."
        ),
        followup_epistemic_purpose=(
            "Propose the next-round follow-up action inside the preregistered round splits."
        ),
        candidate_outcomes=("outcome.inconclusive", "outcome.negative", "outcome.positive"),
        requested_authority_class="scientific_execution_authorization",
        cost_screening_policy_sha256=qualification["policies"]["cost-screening"],
        risk_screening_policy_sha256=qualification["policies"]["risk-screening"],
    )
    compilation_policy = ProtocolCompilationPolicyPin(
        capability_catalog_sha256=_sha256_bytes(_read_bytes(capability_catalog)),
        resource_catalog_sha256=_sha256_bytes(_read_bytes(resource_catalog)),
        compiler_implementation_sha256=_sha256_bytes(
            _read_bytes(release / "aletheia" / "protocols" / "compiler.py")
        ),
        allowed_protocol_author_principal_ids=(SERVICE_PRINCIPALS["protocol_compilation"],),
        action_category_policies=(
            ActionProtocolCategoryPolicy(
                action_kind=ActionKind.DISCRIMINATE,
                allowed_categories=(ProtocolActionCategory.DETERMINISTIC_ANALYSIS,),
            ),
        ),
        world_model_required_action_kinds=(ActionKind.DISCRIMINATE,),
    )
    assessment_policy = ContinuationAssessmentPolicyPin(
        assessment_implementation_sha256=_sha256_bytes(
            _read_bytes(release / "aletheia" / "research_controller" / "continuation_assessor.py")
        ),
        observed_outcome_identity_policy_sha256=OBSERVED_OUTCOME_IDENTITY_POLICY_SHA256,
        allowed_assessor_principal_ids=(SERVICE_PRINCIPALS["continuation_assessment"],),
        allowed_fit_rule_sha256s=(EXACT_OUTCOME_BIN_FIT_RULE_SHA256,),
    )
    policy_pins = {
        "proposal": proposal_policy,
        "compilation": compilation_policy,
        "assessment": assessment_policy,
    }

    # ---- policy documents for the non-primary services ---------------------
    service_policies = {
        service: policy_doc(
            service_policy_root,
            f"{_service_dir_name(service)}-policy",
            f"service:{service}",
            f"Operational policy for the {service} RPC service in the ARL-2 dry-run window.",
        )
        for service in (
            "execution_registration",
            "raw_run_source",
            "committed_validation_source",
            "atomic_admission",
            "action_kernel_command",
            "transition_kernel_command",
            CUPRATE_SERVICE,
        )
    }

    # ---- per-service pin identity (principal + policy sha) ------------------
    primary_identity = {
        "action_proposal": (SERVICE_PRINCIPALS["action_proposal"], proposal_policy.policy_sha256),
        "protocol_compilation": (
            SERVICE_PRINCIPALS["protocol_compilation"],
            compilation_policy.policy_sha256,
        ),
        "execution_authorization": (BRIDGE_PRINCIPALS["execution"], bridge_policies["execution"]),
        "database_observation": (BRIDGE_PRINCIPALS["database"], bridge_policies["database"]),
        "independent_validation": (BRIDGE_PRINCIPALS["validator"], bridge_policies["validator"]),
        "independent_admission": (BRIDGE_PRINCIPALS["admission"], bridge_policies["admission"]),
        "continuation_assessment": (
            SERVICE_PRINCIPALS["continuation_assessment"],
            assessment_policy.policy_sha256,
        ),
    }

    def pin_identity(service: str) -> tuple[str, str]:
        if service in primary_identity:
            return primary_identity[service]
        return SERVICE_PRINCIPALS[service], service_policies[service]

    # ---- identity manifests (single source of truth for the window) --------
    manifest_shas: dict[str, str] = {}
    manifest_paths: dict[str, str] = {}
    for service in ALL_SERVICES:
        principal, policy_sha = pin_identity(service)
        entry = {
            "schema_name": "aletheia.arl2_service_identity_manifest",
            "schema_version": 1,
            "window_id": window_id,
            "service": service,
            "principal_id": principal,
            "policy_sha256": policy_sha,
            "receipt_key_id": keys["receipt"][service]["key_id"],
            "socket_path": str(layout["sockets"][service] / f"{_service_dir_name(service)}.sock"),
            "prepared_at": _iso(prepared_at),
        }
        path = layout["identity_manifests"] / f"{_service_dir_name(service)}.json"
        manifest_paths[service] = str(path)
        manifest_shas[service] = _write_canonical(
            path, canonical_json_bytes(entry), mode=0o644, uid=driver_uid, gid=driver_gid
        )
    worker_entry = {
        "schema_name": "aletheia.arl2_service_identity_manifest",
        "schema_version": 1,
        "window_id": window_id,
        "service": "worker",
        "principal_id": WORKER_PRINCIPAL,
        "prepared_at": _iso(prepared_at),
    }
    worker_manifest_path = layout["identity_manifests"] / "worker.json"
    worker_manifest_sha = _write_canonical(
        worker_manifest_path,
        canonical_json_bytes(worker_entry),
        mode=0o644,
        uid=driver_uid,
        gid=driver_gid,
    )

    # ---- authority bindings (the eight step roles + two command roles) ------
    ordinary = activation["ordinary"]

    def make_binding(role, *, principal_id, policy_sha256, manifest_sha, key_id=None):
        return ControllerStepAuthorityBinding(
            role=role,
            principal_id=principal_id,
            key_id=key_id,
            policy_sha256=policy_sha256,
            service_manifest_sha256=manifest_sha,
            externally_deployed=True,
        )

    bindings = {
        "action_proposal": make_binding(
            ControllerStepAuthorityRole.ACTION_PROPOSAL,
            principal_id=SERVICE_PRINCIPALS["action_proposal"],
            policy_sha256=proposal_policy.policy_sha256,
            manifest_sha=manifest_shas["action_proposal"],
        ),
        "protocol_compilation": make_binding(
            ControllerStepAuthorityRole.PROTOCOL_COMPILATION,
            principal_id=SERVICE_PRINCIPALS["protocol_compilation"],
            policy_sha256=compilation_policy.policy_sha256,
            manifest_sha=manifest_shas["protocol_compilation"],
        ),
        "execution_authorization": make_binding(
            ControllerStepAuthorityRole.EXECUTION_AUTHORIZATION,
            principal_id=bridge["execution"].principal_id,
            policy_sha256=bridge["execution"].policy_sha256,
            manifest_sha=manifest_shas["execution_authorization"],
            key_id=bridge["execution"].key_id,
        ),
        "database_attestation": make_binding(
            ControllerStepAuthorityRole.DATABASE_ATTESTATION,
            principal_id=bridge["database"].principal_id,
            policy_sha256=bridge["database"].policy_sha256,
            manifest_sha=manifest_shas["database_observation"],
            key_id=bridge["database"].key_id,
        ),
        "independent_validation": make_binding(
            ControllerStepAuthorityRole.INDEPENDENT_VALIDATION,
            principal_id=bridge["validator"].principal_id,
            policy_sha256=bridge["validator"].policy_sha256,
            manifest_sha=manifest_shas["independent_validation"],
            key_id=bridge["validator"].key_id,
        ),
        "independent_admission": make_binding(
            ControllerStepAuthorityRole.INDEPENDENT_ADMISSION,
            principal_id=bridge["admission"].principal_id,
            policy_sha256=bridge["admission"].policy_sha256,
            manifest_sha=manifest_shas["independent_admission"],
            key_id=bridge["admission"].key_id,
        ),
        "kernel_command": make_binding(
            ControllerStepAuthorityRole.KERNEL_COMMAND,
            principal_id=ordinary["principal_id"],
            policy_sha256=activation["policy_sha256"],
            manifest_sha=manifest_shas["atomic_admission"],
            key_id=ordinary["key_id"],
        ),
        "continuation_assessment": make_binding(
            ControllerStepAuthorityRole.CONTINUATION_ASSESSMENT,
            principal_id=SERVICE_PRINCIPALS["continuation_assessment"],
            policy_sha256=assessment_policy.policy_sha256,
            manifest_sha=manifest_shas["continuation_assessment"],
        ),
        "action_kernel_command": make_binding(
            ControllerStepAuthorityRole.ACTION_KERNEL_COMMAND,
            principal_id=ordinary["principal_id"],
            policy_sha256=activation["policy_sha256"],
            manifest_sha=manifest_shas["action_kernel_command"],
            key_id=ordinary["key_id"],
        ),
        "transition_kernel_command": make_binding(
            ControllerStepAuthorityRole.TRANSITION_KERNEL_COMMAND,
            principal_id=ordinary["principal_id"],
            policy_sha256=activation["policy_sha256"],
            manifest_sha=manifest_shas["transition_kernel_command"],
            key_id=ordinary["key_id"],
        ),
    }

    # ---- the fourteen service pins ------------------------------------------
    def service_pin(service: str) -> ControllerWorkerRPCServicePin:
        principal, policy_sha = pin_identity(service)
        role_names = _SERVICE_BINDING_ROLES.get(service, ())
        binding_shas = tuple(sorted(bindings[name].binding_sha256 for name in role_names))
        receipt = keys["receipt"][service]
        return ControllerWorkerRPCServicePin(
            service_principal_id=principal,
            service_manifest_sha256=manifest_shas[service],
            service_policy_sha256=policy_sha,
            operations=operations[service],
            authority_binding_sha256s=binding_shas,
            socket_path=str(layout["sockets"][service] / f"{_service_dir_name(service)}.sock"),
            socket_owner_uid=service_uid[service],
            socket_group_gid=driver_gid,
            socket_mode=0o660,
            peer_uid=service_uid[service],
            peer_gid=driver_gid,
            receipt_key_id=receipt["key_id"],
            receipt_public_key_ed25519_hex=receipt["public_hex"],
            valid_from=valid_from,
            expires_at=expires_at,
            connect_timeout_seconds=2.0,
            max_request_bytes=max_bytes[service],
            max_response_bytes=max_bytes[service],
        )

    pins = {service: service_pin(service) for service in WORKER_SERVICES}
    driver_pins = {service: service_pin(service) for service in COMMAND_SERVICES}
    capability_pin = service_pin(CUPRATE_SERVICE)

    return {
        "pins": pins,
        "driver_pins": driver_pins,
        "capability_pin": capability_pin,
        "bindings": bindings,
        "bridge": bridge,
        "bridge_policies": bridge_policies,
        "policy_pins": policy_pins,
        "service_policies": service_policies,
        "identity": {
            "manifests": manifest_shas,
            "manifest_paths": manifest_paths,
            "worker_manifest_sha256": worker_manifest_sha,
            "worker_manifest_path": str(worker_manifest_path),
        },
        "catalogs": {
            "capability": _sha256_bytes(_read_bytes(capability_catalog)),
            "resource": _sha256_bytes(_read_bytes(resource_catalog)),
        },
    }


# --------------------------------------------------------------------------
# Controller manifest
# --------------------------------------------------------------------------


def _build_controller_manifest(
    layout,
    *,
    release,
    prepared_at,
    window_id,
    worker_manifest_sha256,
    capability_catalog_sha256,
    driver_uid,
    driver_gid,
):
    from aletheia.durable_tasks.contracts import RetryPolicy
    from aletheia.research_controller.contracts import ResearchControllerManifest
    from aletheia.research_kernel.schemas import canonical_json_bytes

    def policy_doc(name: str, policy: str) -> str:
        path = layout["controller_policies"] / f"{name}.json"
        payload = canonical_json_bytes(_policy_note(window_id, f"controller:{name}", prepared_at, policy))
        return _write_canonical(path, payload, mode=0o644, uid=driver_uid, gid=driver_gid)

    entrypoint = release / "scripts" / "run_research_controller_runtime.py"
    manifest = ResearchControllerManifest(
        controller_key=CONTROLLER_PRINCIPAL,
        controller_code_sha256=_sha256_bytes(_read_bytes(entrypoint)),
        controller_policy_sha256=policy_doc(
            "controller-policy",
            "Research-controller operating policy for the ARL-2 dry-run window.",
        ),
        capability_catalog_sha256=capability_catalog_sha256,
        protocol_registry_policy_sha256=policy_doc(
            "protocol-registry",
            "Protocol registry policy for the ARL-2 dry-run window.",
        ),
        scientific_bridge_policy_sha256=policy_doc(
            "scientific-bridge",
            "Scientific-bridge policy for the ARL-2 dry-run window.",
        ),
        worker_manifest_sha256=worker_manifest_sha256,
        retry_policy=RetryPolicy(max_attempts=3, lease_seconds=60, heartbeat_interval_seconds=10),
        prepared_at=prepared_at,
    )
    path = layout["configs"] / "arl2-controller-manifest.json"
    file_sha256 = _write_canonical(
        path, canonical_json_bytes(manifest), mode=0o644, uid=driver_uid, gid=driver_gid
    )
    return manifest, {
        "path": str(path),
        "file_sha256": file_sha256,
        "manifest_sha256": manifest.manifest_sha256,
        "controller_id": manifest.controller_id,
    }


# --------------------------------------------------------------------------
# Worker composition
# --------------------------------------------------------------------------

# Step -> adapter roles, in the order _ACTIVE_STEP_ROLES fixes (sorted by
# role value); step_executor.py is the authority.
_STEP_ROLE_NAMES: dict[str, tuple[str, ...]] = {
    "propose_action": ("action_proposal",),
    "compile_protocol": ("protocol_compilation",),
    "propose_redesign": ("action_proposal",),
    "register_execution": ("execution_authorization",),
    "commit_validation": ("database_attestation", "independent_validation"),
    "commit_admission": ("database_attestation", "independent_admission", "kernel_command"),
    "derive_continuation": ("continuation_assessment",),
    "propose_followup": ("action_proposal",),
}


def _build_worker_composition(
    layout,
    *,
    release,
    controller_manifest,
    controller_paths,
    closure,
    qualification,
    activation,
    cas_metadata,
    prepared_at,
    database_url_sha256,
    schema_revision,
    driver_uid,
    driver_gid,
) -> dict:
    from aletheia.research_controller.contracts import ControllerStep
    from aletheia.research_controller.step_executor import (
        ControllerStepAdapterManifest,
        ControllerStepAdapterSetManifest,
    )
    from aletheia.research_controller.worker_composition import (
        ControllerWorkerRPCServiceSet,
        ResearchControllerWorkerRuntimeConfig,
        ResearchKernelReadOnlyConfig,
        controller_step_adapter_source_sha256,
        controller_step_rpc_configuration_sha256,
    )
    from aletheia.research_kernel.schemas import canonical_json_bytes

    kernel_reader = ResearchKernelReadOnlyConfig(
        trust_root=activation["trust_root"],
        cas_root=str(activation["cas_root"]),
        cas_owner_uid=cas_metadata["uid"],
        cas_group_gid=cas_metadata["gid"],
        cas_device_id=cas_metadata["device_id"],
        cas_inode=cas_metadata["inode"],
        cas_directory_mode=cas_metadata["mode"],
        max_object_bytes=MAX_OBJECT_BYTES,
    )
    rpc_services = ControllerWorkerRPCServiceSet(**closure["pins"])

    bindings = closure["bindings"]
    adapters = []
    for step_name, role_names in sorted(_STEP_ROLE_NAMES.items()):
        step = getattr(ControllerStep, step_name.upper())
        authorities = tuple(
            bindings[name] for name in sorted(role_names, key=lambda n: bindings[n].role.value)
        )
        adapters.append(
            ControllerStepAdapterManifest(
                step=step,
                adapter_code_sha256=controller_step_adapter_source_sha256(
                    step, reviewed_code_root=str(release)
                ),
                adapter_config_sha256=controller_step_rpc_configuration_sha256(step, rpc_services),
                authorities=authorities,
                prepared_at=prepared_at,
            )
        )
    adapter_set = ControllerStepAdapterSetManifest(
        controller_id=controller_paths["controller_id"],
        controller_manifest_sha256=controller_paths["manifest_sha256"],
        worker_manifest_sha256=closure["identity"]["worker_manifest_sha256"],
        worker_process_principal_id=WORKER_PRINCIPAL,
        adapters=tuple(adapters),
        prepared_at=prepared_at,
    )
    config = ResearchControllerWorkerRuntimeConfig(
        role="worker",
        process_principal_id=WORKER_PRINCIPAL,
        controller_id=controller_paths["controller_id"],
        controller_manifest_sha256=controller_paths["manifest_sha256"],
        database_url_sha256=database_url_sha256,
        schema_revision=schema_revision,
        adapter_set_manifest=adapter_set,
        rpc_services=rpc_services,
        kernel_reader=kernel_reader,
        terminal_reader=qualification["reader"],
        prepared_at=prepared_at,
    )
    path = layout["role_configs"] / "worker.json"
    file_sha256 = _write_canonical(
        path, canonical_json_bytes(config), mode=0o644, uid=driver_uid, gid=driver_gid
    )
    return {
        "config": config,
        "path": str(path),
        "file_sha256": file_sha256,
        "adapter_set_id": adapter_set.adapter_set_id,
    }


# --------------------------------------------------------------------------
# Role deployments
# --------------------------------------------------------------------------


def _build_role_deployments(
    layout,
    *,
    release,
    controller_manifest,
    controller_paths,
    qualification,
    worker,
    prepared_at,
    database_url_sha256,
    schema_revision,
    driver_uid,
    driver_gid,
) -> dict:
    from aletheia.execution.terminal_runtime import QualificationTerminalRuntimeConfig
    from aletheia.research_controller_runtime import (
        ResearchControllerRuntimeDeployment,
        ResearchControllerRuntimeRole,
    )
    from aletheia.research_kernel.schemas import canonical_json_bytes

    def module_source(module_name: str) -> tuple[str, str]:
        path = release / "aletheia" / f"{module_name.rsplit('.', 1)[-1]}.py"
        return str(path), _sha256_bytes(_read_bytes(path))

    def write_role_config(name: str, payload: bytes) -> tuple[str, str]:
        path = layout["role_configs"] / f"{name}.json"
        return str(path), _write_canonical(path, payload, mode=0o644, uid=driver_uid, gid=driver_gid)

    def postgres_config(role: str, principal: str) -> bytes:
        return (
            json.dumps(
                {
                    "schema_name": "aletheia.research_controller_postgresql_runtime_config",
                    "schema_version": 1,
                    "role": role,
                    "process_principal_id": principal,
                    "database_url_sha256": database_url_sha256,
                    "schema_revision": schema_revision,
                    "scientific_authority": False,
                    "kernel_command_authority": False,
                    "observation_admission_authority": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    # The terminal runtime config is the reader R plus the dispatcher role
    # fields (terminal_runtime.py:215-223); one prepared_at throughout.
    terminal_payload = dict(qualification["reader_json"])
    terminal_payload.update(
        {
            "schema_name": "aletheia.qualification_terminal_runtime_config",
            "role": "terminal_dispatcher",
            "process_principal_id": ROLE_PRINCIPALS["terminal_dispatcher"],
            "controller_manifest_sha256": controller_paths["manifest_sha256"],
            "database_url_sha256": database_url_sha256,
            "schema_revision": schema_revision,
        }
    )
    terminal_config = QualificationTerminalRuntimeConfig.model_validate(terminal_payload)

    configs: dict[str, tuple[str, str]] = {}
    configs["kernel_dispatcher"] = write_role_config(
        "kernel_dispatcher",
        postgres_config("kernel_dispatcher", ROLE_PRINCIPALS["kernel_dispatcher"]),
    )
    configs["delivery_reconciler"] = write_role_config(
        "delivery_reconciler",
        postgres_config("delivery_reconciler", ROLE_PRINCIPALS["delivery_reconciler"]),
    )
    configs["terminal_dispatcher"] = write_role_config(
        "terminal_dispatcher",
        (
            json.dumps(
                terminal_config.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8"),
    )
    configs["worker"] = (worker["path"], worker["file_sha256"])

    factories = {
        "kernel_dispatcher": (
            "aletheia.research_controller_postgresql_runtime",
            "build_postgresql_runtime",
        ),
        "terminal_dispatcher": (
            "aletheia.research_controller_terminal_runtime",
            "build_terminal_runtime",
        ),
        "worker": ("aletheia.research_controller_worker_runtime", "build_worker_runtime"),
        "delivery_reconciler": (
            "aletheia.research_controller_postgresql_runtime",
            "build_postgresql_runtime",
        ),
    }

    roles: dict[str, dict] = {}
    for role in ROLE_ORDER:
        module_name, attribute = factories[role]
        factory_path, factory_sha = module_source(module_name)
        config_path, config_sha = configs[role]
        deployment = ResearchControllerRuntimeDeployment(
            role=ResearchControllerRuntimeRole[role.upper()],
            controller_manifest_path=controller_paths["path"],
            controller_manifest_file_sha256=controller_paths["file_sha256"],
            controller_manifest_sha256=controller_paths["manifest_sha256"],
            reviewed_code_root=str(release),
            composition_factory_module=module_name,
            composition_factory_attribute=attribute,
            composition_factory_source_path=factory_path,
            composition_factory_source_sha256=factory_sha,
            composition_config_path=config_path,
            composition_config_file_sha256=config_sha,
            process_principal_id=ROLE_PRINCIPALS[role],
            prepared_at=prepared_at,
        )
        path = layout["role_configs"] / f"{role}-deployment.json"
        file_sha256 = _write_canonical(
            path, canonical_json_bytes(deployment), mode=0o644, uid=driver_uid, gid=driver_gid
        )
        roles[role] = {
            "deployment": deployment,
            "path": str(path),
            "file_sha256": file_sha256,
            "runtime_id": deployment.runtime_id,
        }
    return roles


# --------------------------------------------------------------------------
# Service configs and deployments
# --------------------------------------------------------------------------


def _build_service_deployments(
    layout,
    *,
    release,
    controller_manifest,
    controller_paths,
    closure,
    keys,
    qualification,
    activation,
    request,
    worker,
    cas_metadata,
    prepared_at,
    database_url_sha256,
    schema_revision,
    service_uid,
    driver_uid,
    worker_uid,
    driver_gid,
) -> dict:
    """Author the eleven live configs/deployments (three deferred services skipped)."""

    from aletheia.execution.registration_custody import QualificationExecutionRegistrationConfig
    from aletheia.observations.kernel_authority import ObservationKernelPolicyAssignment
    from aletheia.research_kernel.commands import ResearchScopeBinding
    from aletheia.research_controller.kernel_authority import ControllerKernelPolicyAssignment
    from aletheia.research_controller_rpc_runtime import ControllerWorkerRPCServerDeployment
    from aletheia.research_kernel.schemas import canonical_json_bytes

    bindings = closure["bindings"]

    def binding_json(name: str) -> dict:
        return bindings[name].model_dump(mode="json")

    def bridge_json(name: str) -> dict:
        return closure["bridge"][name].model_dump(mode="json")

    def source_pin(relative: str) -> tuple[str, str]:
        path = release / "aletheia" / relative
        return str(path), _sha256_bytes(_read_bytes(path))

    kernel_reader_json = worker["config"].kernel_reader.model_dump(mode="json")
    trust_root_json = activation["trust_root"].model_dump(mode="json")
    reader_json = qualification["reader_json"]
    artifact_authority_json = qualification["artifact_authority_json"]

    archive_metadata = _stat_directory(layout["validation_archive_root"])
    archive_json = {
        "root": str(layout["validation_archive_root"]),
        "owner_uid": archive_metadata["uid"],
        "group_gid": archive_metadata["gid"],
        "device_id": archive_metadata["device_id"],
        "inode": archive_metadata["inode"],
        "directory_mode": 0o700,
        "validator_manifest_sha256": closure["identity"]["manifests"]["independent_validation"],
        "read_only": True,
        "campaign_publication_allowed": False,
    }

    spool_metadata = _stat_directory(layout["action_proposal_spool_root"])
    ca_artifact_metadata = _stat_directory(layout["continuation_artifact_root"])

    registration = QualificationExecutionRegistrationConfig(
        qualification_custody=qualification["custody"],
        runtime_control_authority_pin=qualification["runtime_pin"],
        node_authorities=(qualification["node_authority"],),
        allowed_rate_card_sha256s=(qualification["rate_card_sha256"],),
        allowed_currency_codes=(CURRENCY_CODE,),
        allocator_principal_id=QUALIFICATION_PRINCIPALS["allocator"],
        prepared_at=prepared_at,
    )

    scope_binding = ResearchScopeBinding(quest_id=activation["quest_id"])
    controller_assignment = ControllerKernelPolicyAssignment(
        quest_id=activation["quest_id"],
        scope_binding=scope_binding,
        authorization_policy=activation["policy"],
    )
    observation_assignment = ObservationKernelPolicyAssignment(
        quest_id=activation["quest_id"],
        scope_binding=scope_binding,
        authorization_policy=activation["policy"],
    )

    def header(service: str, *, with_database: bool = True) -> dict:
        pin = _pin_for(closure, service)
        payload = {
            "controller_id": controller_paths["controller_id"],
            "controller_manifest_sha256": controller_paths["manifest_sha256"],
            "worker_process_principal_id": WORKER_PRINCIPAL,
            "service_id": pin.service_id,
            "service_pin_sha256": pin.pin_sha256,
        }
        if with_database:
            payload["database_url_sha256"] = database_url_sha256
            payload["schema_revision"] = schema_revision
        return payload

    provider_path, provider_sha = source_pin("research_controller/action_proposal_provider.py")
    registrar_path, registrar_sha = source_pin("observations/execution_registration.py")
    adapters_path, adapters_sha = source_pin("observations/adapters.py")
    observation_service_path, observation_service_sha = source_pin("observations/service.py")
    admission_service_path, admission_service_sha = source_pin("observations/admission_service.py")
    coordinator_path, coordinator_sha = source_pin("observations/coordinator.py")
    observation_authority_path, observation_authority_sha = source_pin(
        "observations/kernel_authority.py"
    )
    controller_authority_path, controller_authority_sha = source_pin(
        "research_controller/kernel_authority.py"
    )
    assessor_path, assessor_sha = source_pin("research_controller/continuation_assessor.py")
    cuprate_source_path, cuprate_source_sha = source_pin("execution/cuprate/service.py")

    def sorted_binding_jsons(role_names: tuple[str, ...]) -> list[dict]:
        chosen = sorted((bindings[name] for name in role_names), key=lambda b: b.binding_sha256)
        return [binding.model_dump(mode="json") for binding in chosen]

    configs: dict[str, dict] = {}

    configs["action_proposal"] = {
        "schema_name": "aletheia.action_proposal_rpc_service_config",
        "schema_version": 1,
        **header("action_proposal"),
        "kernel_reader": kernel_reader_json,
        "authority_binding": binding_json("action_proposal"),
        "proposal_policy": closure["policy_pins"]["proposal"].model_dump(mode="json"),
        "provider_implementation_source_path": provider_path,
        "provider_implementation_source_sha256": provider_sha,
        "submission_spool_root": {
            "path": str(layout["action_proposal_spool_root"]),
            "owner_uid": spool_metadata["uid"],
            "owner_gid": spool_metadata["gid"],
            "device_id": spool_metadata["device_id"],
            "inode": spool_metadata["inode"],
            "directory_mode": spool_metadata["mode"],
        },
        "prepared_at": _iso(prepared_at),
        "direct_scientific_authority": False,
        "kernel_signing_key_loaded": False,
        "observation_signing_key_loaded": False,
        "execution_access_allowed": False,
        "generic_model_callback_allowed": False,
        "cost_or_risk_authority_loaded": False,
    }

    configs["execution_registration"] = {
        "schema_name": "aletheia.execution_registration_rpc_service_config",
        "schema_version": 1,
        **header("execution_registration"),
        "kernel_reader": kernel_reader_json,
        "authority_binding": binding_json("execution_authorization"),
        "execution_authority_pin": bridge_json("execution"),
        "validator_authority_pin": bridge_json("validator"),
        "admission_authority_pin": bridge_json("admission"),
        "qualification_registration": registration.model_dump(mode="json"),
        "registrar_implementation_source_path": registrar_path,
        "registrar_implementation_source_sha256": registrar_sha,
        "prepared_at": _iso(prepared_at),
        "private_domain_signing_key_loaded": False,
        "runtime_control_signing_key_loaded": False,
        "execution_launch_allowed": False,
        "node_registry_mutation_allowed": False,
        "terminal_commit_allowed": False,
        "direct_kernel_mutation_allowed": False,
        "direct_observation_admission_allowed": False,
    }

    configs["raw_run_source"] = {
        "schema_name": "aletheia.raw_run_source_rpc_service_config",
        "schema_version": 1,
        **header("raw_run_source"),
        "authority_binding": binding_json("execution_authorization"),
        "execution_authority_pin": bridge_json("execution"),
        "validator_authority_pin": bridge_json("validator"),
        "admission_authority_pin": bridge_json("admission"),
        "qualification_reader": reader_json,
        "source_implementation_source_path": adapters_path,
        "source_implementation_source_sha256": adapters_sha,
        "prepared_at": _iso(prepared_at),
        "private_domain_signing_key_loaded": False,
        "execution_mutation_allowed": False,
        "database_mutation_allowed": False,
        "validation_allowed": False,
        "direct_observation_admission_allowed": False,
        "direct_kernel_mutation_allowed": False,
    }

    database_key = keys["domain"]["database"]
    configs["database_observation"] = {
        "schema_name": "aletheia.database_observation_rpc_service_config",
        "schema_version": 1,
        **header("database_observation"),
        "kernel_reader": kernel_reader_json,
        "authority_binding": binding_json("database_attestation"),
        "database_authority_pin": bridge_json("database"),
        "database_signing_key": {
            "path": database_key["path"],
            "file_sha256": database_key["file_sha256"],
            "key_id": database_key["key_id"],
            "owner_uid": service_uid["database_observation"],
            "owner_gid": driver_gid,
            "file_mode": 0o400,
        },
        "execution_authority_pin": bridge_json("execution"),
        "validator_authority_pin": bridge_json("validator"),
        "admission_authority_pin": bridge_json("admission"),
        "qualification_reader": reader_json,
        "artifact_verification_authority": artifact_authority_json,
        "validation_archive": archive_json,
        "challenge_ttl_seconds": 300,
        "service_implementation_source_path": observation_service_path,
        "service_implementation_source_sha256": observation_service_sha,
        "prepared_at": _iso(prepared_at),
        "database_attestation_signing_key_loaded": True,
        "validator_signing_key_loaded": False,
        "admission_signing_key_loaded": False,
        "kernel_signing_key_loaded": False,
        "execution_mutation_allowed": False,
        "direct_observation_admission_allowed": False,
        "direct_kernel_mutation_allowed": False,
    }

    configs["committed_validation_source"] = {
        "schema_name": "aletheia.committed_validation_source_rpc_service_config",
        "schema_version": 1,
        **header("committed_validation_source"),
        "kernel_reader": kernel_reader_json,
        "authority_bindings": sorted_binding_jsons(
            ("database_attestation", "independent_validation")
        ),
        "database_authority_pin": bridge_json("database"),
        "execution_authority_pin": bridge_json("execution"),
        "validator_authority_pin": bridge_json("validator"),
        "admission_authority_pin": bridge_json("admission"),
        "qualification_reader": reader_json,
        "artifact_verification_authority": artifact_authority_json,
        "validation_archive": archive_json,
        "source_implementation_source_path": adapters_path,
        "source_implementation_source_sha256": adapters_sha,
        "prepared_at": _iso(prepared_at),
        "private_domain_signing_key_loaded": False,
        "database_mutation_allowed": False,
        "execution_mutation_allowed": False,
        "campaign_publication_allowed": False,
        "validation_receipt_issuance_allowed": False,
        "direct_observation_admission_allowed": False,
        "direct_kernel_mutation_allowed": False,
    }

    admission_key = keys["domain"]["admission"]
    configs["independent_admission"] = {
        "schema_name": "aletheia.independent_admission_rpc_service_config",
        "schema_version": 1,
        **header("independent_admission"),
        "kernel_reader": kernel_reader_json,
        "authority_binding": binding_json("independent_admission"),
        "database_authority_pin": bridge_json("database"),
        "execution_authority_pin": bridge_json("execution"),
        "validator_authority_pin": bridge_json("validator"),
        "admission_authority_pin": bridge_json("admission"),
        "qualification_reader": reader_json,
        "artifact_verification_authority": artifact_authority_json,
        "validation_archive": archive_json,
        "admission_signing_key": {
            "path": admission_key["path"],
            "file_sha256": admission_key["file_sha256"],
            "key_id": admission_key["key_id"],
            "owner_uid": service_uid["independent_admission"],
            "group_gid": driver_gid,
            "file_mode": 0o400,
        },
        "service_implementation_source_path": admission_service_path,
        "service_implementation_source_sha256": admission_service_sha,
        "prepared_at": _iso(prepared_at),
        "admission_signing_key_loaded": True,
        "database_signing_key_loaded": False,
        "execution_signing_key_loaded": False,
        "validator_signing_key_loaded": False,
        "kernel_signing_key_loaded": False,
        "database_mutation_allowed": False,
        "execution_mutation_allowed": False,
        "campaign_publication_allowed": False,
        "validation_receipt_issuance_allowed": False,
        "scientific_slot_commit_allowed": False,
        "direct_kernel_mutation_allowed": False,
    }

    atomic_database_key = keys["domain"]["atomic_database"]
    atomic_kernel_key = keys["domain"]["atomic_kernel"]
    configs["atomic_admission"] = {
        "schema_name": "aletheia.atomic_admission_rpc_service_config",
        "schema_version": 1,
        **header("atomic_admission"),
        "kernel": {
            "trust_root": trust_root_json,
            "cas_root": str(activation["cas_root"]),
            "cas_owner_uid": cas_metadata["uid"],
            "cas_group_gid": cas_metadata["gid"],
            "cas_device_id": cas_metadata["device_id"],
            "cas_inode": cas_metadata["inode"],
            "cas_directory_mode": cas_metadata["mode"],
            "max_object_bytes": MAX_OBJECT_BYTES,
            "read_only": False,
            "snapshot_archive_write_allowed": True,
            "arbitrary_object_admission_allowed": False,
        },
        "kernel_policy_assignments": [observation_assignment.model_dump(mode="json")],
        "authority_bindings": sorted_binding_jsons(
            ("database_attestation", "independent_admission", "kernel_command")
        ),
        "database_authority_pin": bridge_json("database"),
        "execution_authority_pin": bridge_json("execution"),
        "validator_authority_pin": bridge_json("validator"),
        "admission_authority_pin": bridge_json("admission"),
        "qualification_reader": reader_json,
        "artifact_verification_authority": artifact_authority_json,
        "validation_archive": archive_json,
        "database_signing_key": {
            "path": atomic_database_key["path"],
            "file_sha256": atomic_database_key["file_sha256"],
            "key_id": atomic_database_key["key_id"],
            "owner_uid": service_uid["atomic_admission"],
            "group_gid": driver_gid,
            "file_mode": 0o400,
        },
        "kernel_signing_key": {
            "path": atomic_kernel_key["path"],
            "file_sha256": atomic_kernel_key["file_sha256"],
            "key_id": atomic_kernel_key["key_id"],
            "owner_uid": service_uid["atomic_admission"],
            "group_gid": driver_gid,
            "file_mode": 0o400,
        },
        "coordinator_source_path": coordinator_path,
        "coordinator_source_sha256": coordinator_sha,
        "kernel_authority_source_path": observation_authority_path,
        "kernel_authority_source_sha256": observation_authority_sha,
        "prepared_at": _iso(prepared_at),
        "database_signing_key_loaded": True,
        "kernel_signing_key_loaded": True,
        "admission_signing_key_loaded": False,
        "execution_signing_key_loaded": False,
        "validator_signing_key_loaded": False,
        "admission_row_and_kernel_commit_atomic": True,
        "independent_decision_required": True,
        "arbitrary_kernel_event_allowed": False,
        "campaign_publication_allowed": False,
        "execution_mutation_allowed": False,
    }

    configs["continuation_assessment"] = {
        "schema_name": "aletheia.continuation_assessment_rpc_service_config",
        "schema_version": 1,
        **header("continuation_assessment"),
        "kernel_reader": kernel_reader_json,
        "authority_binding": binding_json("continuation_assessment"),
        "assessment_policy": closure["policy_pins"]["assessment"].model_dump(mode="json"),
        "assessment_implementation_source_path": assessor_path,
        "assessment_implementation_source_sha256": assessor_sha,
        "artifact_root": {
            "path": str(layout["continuation_artifact_root"]),
            "owner_uid": ca_artifact_metadata["uid"],
            "owner_gid": ca_artifact_metadata["gid"],
            "device_id": ca_artifact_metadata["device_id"],
            "inode": ca_artifact_metadata["inode"],
            "directory_mode": 0o700,
        },
        "prepared_at": _iso(prepared_at),
        "direct_scientific_authority": False,
        "kernel_signing_key_loaded": False,
        "observation_signing_key_loaded": False,
        "execution_access_allowed": False,
        "generic_model_callback_allowed": False,
    }

    for service in COMMAND_SERVICES:
        key = keys["domain"][service]
        configs[service] = {
            "controller_id": controller_paths["controller_id"],
            "controller_manifest_sha256": controller_paths["manifest_sha256"],
            "worker_process_principal_id": WORKER_PRINCIPAL,
            "service_id": closure["driver_pins"][service].service_id,
            "service_pin_sha256": closure["driver_pins"][service].pin_sha256,
            "prepared_at": _iso(prepared_at),
            "authorization_key_id": activation["ordinary"]["key_id"],
            "kernel_authority_source_path": controller_authority_path,
            "kernel_authority_source_sha256": controller_authority_sha,
            "trust_root": trust_root_json,
            "policy_assignments": [controller_assignment.model_dump(mode="json")],
            "command_signing_key": {
                "path": key["path"],
                "file_sha256": key["file_sha256"],
                "key_id": key["key_id"],
                "owner_uid": service_uid[service],
                "group_gid": driver_gid,
                "file_mode": 0o400,
            },
        }

    configs[CUPRATE_SERVICE] = {
        "schema_name": "aletheia.cuprate_diagnostic_rpc_service_config",
        "schema_version": 1,
        **header(CUPRATE_SERVICE, with_database=False),
        # fail here rather than at cuprate service start: the service runs as
        # a distinct uid and its staged_dataset_custody gate rejects any
        # write bit on the dataset file (a 0600 mkstemp staging would EACCES
        # first anyway)
        "dataset_csv_path": request["dataset_csv_path"],
        "dataset_content_sha256": request["dataset_content_sha256"],
        "source_implementation_source_path": cuprate_source_path,
        "source_implementation_source_sha256": cuprate_source_sha,
        "prepared_at": _iso(prepared_at),
        "private_domain_signing_key_loaded": False,
        "database_access_allowed": False,
        "execution_mutation_allowed": False,
        "validation_allowed": False,
        "direct_observation_admission_allowed": False,
        "direct_kernel_mutation_allowed": False,
        "network_egress_allowed": False,
    }

    # The three live-keyed services are deferred to their per-round pause;
    # their identity is already commissioned inside the closure above.
    for service in DEFERRED_SERVICES:
        if service in configs:
            _fail(f"deferred service {service} must not be authored here")

    # ---- write configs and deployments ---------------------------------------
    services: dict[str, dict] = {}
    for service, config in configs.items():
        pin = _pin_for(closure, service)
        config_path = layout["service_configs"] / f"{_service_dir_name(service)}.json"
        config_sha = _write_canonical(
            config_path,
            canonical_json_bytes(config),
            mode=0o440,
            uid=service_uid[service],
            gid=driver_gid,
        )

        module_name, attribute = _SERVICE_FACTORIES[service]
        factory_path = release / "aletheia" / f"{module_name.rsplit('.', 1)[-1]}.py"
        socket_metadata = _stat_directory(layout["sockets"][service])
        deployment = ControllerWorkerRPCServerDeployment(
            service_pin=pin,
            controller_id=controller_paths["controller_id"],
            controller_manifest_sha256=controller_paths["manifest_sha256"],
            worker_process_principal_id=WORKER_PRINCIPAL,
            worker_peer_uid=worker_uid,
            worker_peer_gid=driver_gid,
            process_uid=service_uid[service],
            process_gid=driver_gid,
            socket_parent_path=str(layout["sockets"][service]),
            socket_parent_owner_uid=socket_metadata["uid"],
            socket_parent_owner_gid=socket_metadata["gid"],
            socket_parent_mode=socket_metadata["mode"],
            socket_parent_device_id=socket_metadata["device_id"],
            socket_parent_inode=socket_metadata["inode"],
            receipt_private_key_path=keys["receipt"][service]["path"],
            receipt_private_key_sha256=keys["receipt"][service]["file_sha256"],
            reviewed_code_root=str(release),
            composition_factory_module=module_name,
            composition_factory_attribute=attribute,
            composition_factory_source_path=str(factory_path),
            composition_factory_source_sha256=_sha256_bytes(_read_bytes(factory_path)),
            composition_config_path=str(config_path),
            composition_config_file_sha256=config_sha,
            prepared_at=prepared_at,
        )
        deployment_path = layout["service_configs"] / f"{_service_dir_name(service)}-deployment.json"
        deployment_sha = _write_canonical(
            deployment_path,
            canonical_json_bytes(deployment),
            mode=0o644,
            uid=driver_uid,
            gid=driver_gid,
        )
        services[service] = {
            "config_path": str(config_path),
            "config_file_sha256": config_sha,
            "deployment_path": str(deployment_path),
            "deployment_file_sha256": deployment_sha,
            "service_id": pin.service_id,
            "service_pin_sha256": pin.pin_sha256,
            "socket_path": pin.socket_path,
            "identity_manifest_sha256": closure["identity"]["manifests"][service],
        }
    return services


def _pin_for(closure: dict, service: str):
    if service in closure["pins"]:
        return closure["pins"][service]
    if service in closure["driver_pins"]:
        return closure["driver_pins"][service]
    if service == CUPRATE_SERVICE:
        return closure["capability_pin"]
    _fail(f"no commissioned pin for service {service}")


# --------------------------------------------------------------------------
# Driver configuration, deployment, invocation
# --------------------------------------------------------------------------


def _build_driver(
    layout,
    *,
    release,
    activation,
    request,
    controller_manifest,
    controller_paths,
    roles,
    closure,
    prepared_at,
    campaign_deadline,
    database_url,
    database_url_sha256,
    schema_revision,
    driver_user,
    driver_uid,
    worker_uid,
    driver_gid,
) -> tuple:
    from aletheia.arl2_runtime import (
        ARL2KernelCommandServiceSetV1,
        ARL2KernelWriterConfigV1,
        ARL2QuestionCampaignRuntimeConfigV1,
        ARL2QuestionCampaignRuntimeDeploymentV1,
        ARL2RoleInvocationV1,
    )
    from aletheia.research_kernel.schemas import canonical_json_bytes

    cas_metadata = _stat_directory(activation["cas_root"])
    kernel_writer = ARL2KernelWriterConfigV1(
        trust_root=activation["trust_root"],
        cas_root=str(activation["cas_root"]),
        cas_owner_uid=cas_metadata["uid"],
        cas_group_gid=cas_metadata["gid"],
        cas_device_id=cas_metadata["device_id"],
        cas_inode=cas_metadata["inode"],
        max_object_bytes=MAX_OBJECT_BYTES,
    )
    kernel_command_services = ARL2KernelCommandServiceSetV1(
        action_kernel_command=closure["driver_pins"]["action_kernel_command"],
        transition_kernel_command=closure["driver_pins"]["transition_kernel_command"],
    )

    commands = activation["command_files"]
    entrypoint = release / "scripts" / "run_research_controller_runtime.py"

    def source_pin(relative: str) -> tuple[str, str]:
        path = release / "aletheia" / relative
        return str(path), _sha256_bytes(_read_bytes(path))

    replay_path, replay_sha = source_pin("research_controller/campaign_replay.py")
    registration_path, registration_sha = source_pin("protocols/data_registration.py")
    world_model_path, world_model_sha = source_pin("research_controller/world_model_revision.py")
    selection_path, selection_sha = source_pin("research_controller/experiment_selection.py")

    config = ARL2QuestionCampaignRuntimeConfigV1(
        process_principal_id=DRIVER_PRINCIPAL,
        process_uid=driver_uid,
        process_gid=driver_gid,
        controller_id=controller_paths["controller_id"],
        controller_manifest_sha256=controller_paths["manifest_sha256"],
        controller_principal_id=CONTROLLER_PRINCIPAL,
        controller_manifest_path=controller_paths["path"],
        controller_manifest_file_sha256=controller_paths["file_sha256"],
        database_url_sha256=database_url_sha256,
        schema_revision=schema_revision,
        kernel_writer=kernel_writer,
        kernel_command_services=kernel_command_services,
        charter_command_path=commands["charter"][0],
        charter_command_file_sha256=commands["charter"][1],
        problem_command_path=commands["problem"][0],
        problem_command_file_sha256=commands["problem"][1],
        question_command_path=commands["question"][0],
        question_command_file_sha256=commands["question"][1],
        action_proposal_spool_root=str(layout["action_proposal_spool_root"]),
        runtime_entrypoint_path=str(entrypoint),
        runtime_entrypoint_file_sha256=_sha256_bytes(_read_bytes(entrypoint)),
        role_invocations=tuple(
            ARL2RoleInvocationV1(
                role=role,
                deployment_manifest_path=roles[role]["path"],
                deployment_manifest_file_sha256=roles[role]["file_sha256"],
                **(
                    {"process_uid": worker_uid}
                    if role == "worker" and worker_uid != driver_uid
                    else {}
                ),
            )
            for role in ROLE_ORDER
        ),
        bundle_output_root=str(layout["bundle_output_root"]),
        replay_implementation_source_path=replay_path,
        replay_implementation_source_sha256=replay_sha,
        data_registration_source_path=registration_path,
        data_registration_source_sha256=registration_sha,
        world_model_revision_source_path=world_model_path,
        world_model_revision_source_sha256=world_model_sha,
        experiment_selection_source_path=selection_path,
        experiment_selection_source_sha256=selection_sha,
        campaign_deadline=campaign_deadline,
        prepared_at=prepared_at,
    )
    config_path = layout["configs"] / "arl2-driver-configuration.json"
    config_file_sha256 = _write_canonical(
        config_path, canonical_json_bytes(config), mode=0o644, uid=driver_uid, gid=driver_gid
    )

    deployment = ARL2QuestionCampaignRuntimeDeploymentV1(
        configuration_path=str(config_path),
        configuration_file_sha256=config_file_sha256,
        configuration_sha256=config.configuration_sha256,
        request_path=request["request_path"],
        request_file_sha256=request["request_file_sha256"],
        request_sha256=request["request_sha256"],
        process_principal_id=DRIVER_PRINCIPAL,
        process_uid=driver_uid,
        process_gid=driver_gid,
        prepared_at=prepared_at,
        campaign_deadline=campaign_deadline,
    )
    deployment_path = layout["configs"] / "arl2-driver-deployment.json"
    deployment_file_sha256 = _write_canonical(
        deployment_path,
        canonical_json_bytes(deployment),
        mode=0o644,
        uid=driver_uid,
        gid=driver_gid,
    )

    # The driver-control helper consumes this record for start/stop/status;
    # env travels through sudo /usr/bin/env, argv[0] is the control-plane
    # python this script itself runs under.
    logs = layout["working"] / "logs"
    _mkdir_pinned(logs, mode=0o755, uid=driver_uid, gid=driver_gid)
    invocation = {
        "user": driver_user,
        "env": {
            "PATH": "/opt/aletheia/python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONPATH": str(release),
            "PYTHONDONTWRITEBYTECODE": "1",
            "ALETHEIA_DATABASE_URL": database_url,
        },
        "argv": [
            sys.executable,
            str(release / "scripts" / "run-arl2-question-campaign.py"),
            "--deployment-manifest",
            str(deployment_path),
            "--deployment-manifest-sha256",
            deployment_file_sha256,
            "--apply",
            "--acknowledge",
            "RUN_ARL2_QUESTION_CAMPAIGN",
        ],
        "log_path": str(logs / "driver.log"),
    }
    invocation_path = layout["configs"] / "arl2-driver-invocation.json"
    invocation_payload = (json.dumps(invocation, sort_keys=True, indent=2) + "\n").encode("utf-8")
    invocation_file_sha256 = _write_canonical(
        invocation_path, invocation_payload, mode=0o644, uid=driver_uid, gid=driver_gid
    )
    files = {
        "configuration_path": str(config_path),
        "configuration_file_sha256": config_file_sha256,
        "deployment_path": str(deployment_path),
        "deployment_file_sha256": deployment_file_sha256,
    }
    return (
        config,
        deployment,
        files,
        {
            "path": str(invocation_path),
            "file_sha256": invocation_file_sha256,
            "record": invocation,
        },
    )


# --------------------------------------------------------------------------
# Offline verification and the deployment-state file
# --------------------------------------------------------------------------


def _verify_offline(layout, *, services, roles, driver_deployment) -> dict:
    """Re-read every authored manifest through its guarded loader."""

    from aletheia.arl2_runtime import (
        load_arl2_question_campaign_runtime_deployment,
        load_arl2_question_campaign_runtime_inputs,
    )
    from aletheia.research_controller_rpc_runtime import (
        load_controller_worker_rpc_server_deployment,
    )
    from aletheia.research_controller_runtime import load_research_controller_runtime_deployment

    for service, entry in sorted(services.items()):
        load_controller_worker_rpc_server_deployment(
            entry["deployment_path"], expected_file_sha256=entry["deployment_file_sha256"]
        )
    for role, entry in sorted(roles.items()):
        load_research_controller_runtime_deployment(
            entry["path"], expected_file_sha256=entry["file_sha256"]
        )
    deployment = load_arl2_question_campaign_runtime_deployment(
        driver_deployment["deployment_path"],
        expected_file_sha256=driver_deployment["deployment_file_sha256"],
    )
    config, request = load_arl2_question_campaign_runtime_inputs(deployment)
    return {
        "verified_service_deployments": len(services),
        "verified_role_deployments": len(roles),
        "driver_configuration_id": config.configuration_id,
        "driver_deployment_id": deployment.deployment_id,
        "driver_request_id": request.request_id,
    }


def _write_state_file(
    state_path,
    *,
    args,
    working,
    prepared_at,
    valid_from,
    expires_at,
    campaign_deadline,
    activation,
    request,
    cas_metadata,
    database_url_sha256,
    schema_revision,
    controller_manifest,
    controller_paths,
    closure,
    qualification,
    keys,
    service_uid,
    worker,
    roles,
    services,
    driver_config,
    driver_deployment,
    driver_files,
    invocation,
    state_extra,
) -> None:
    from aletheia.research_kernel.schemas import canonical_json_bytes

    cas_root = str(activation["cas_root"])
    state = {
        "schema_name": STATE_SCHEMA,
        "schema_version": 1,
        "window_id": working.name,
        "prepared_at": _iso(prepared_at),
        "valid_from": _iso(valid_from),
        "expires_at": _iso(expires_at),
        "campaign_deadline": _iso(campaign_deadline),
        "quest_id": activation["quest_id"],
        "root_branch_id": activation["root_branch_id"],
        "database_url_sha256": database_url_sha256,
        "schema_revision": schema_revision,
        "driver": {
            "principal": DRIVER_PRINCIPAL,
            "user": args.driver_user,
            "uid": args.driver_uid,
            "gid": args.driver_gid,
            "worker_role_uid": args.worker_uid,
            "configuration_id": driver_config.configuration_id,
            "configuration_path": driver_files["configuration_path"],
            "configuration_file_sha256": driver_files["configuration_file_sha256"],
            "deployment_id": driver_deployment.deployment_id,
            "deployment_path": driver_files["deployment_path"],
            "deployment_file_sha256": driver_files["deployment_file_sha256"],
            "invocation_path": invocation["path"],
            "invocation_file_sha256": invocation["file_sha256"],
        },
        "controller": controller_paths,
        "cas": {**cas_metadata, "root": cas_root},
        "worker": {
            "configuration_path": worker["path"],
            "configuration_file_sha256": worker["file_sha256"],
            "adapter_set_id": worker["adapter_set_id"],
        },
        "roles": {
            role: {
                "runtime_id": entry["runtime_id"],
                "deployment_manifest_path": entry["path"],
                "deployment_manifest_file_sha256": entry["file_sha256"],
                "process_principal_id": ROLE_PRINCIPALS[role],
            }
            for role, entry in roles.items()
        },
        "identity": closure["identity"],
        "bindings": {
            name: {
                "sha256": binding.binding_sha256,
                "json": binding.model_dump(mode="json"),
            }
            for name, binding in closure["bindings"].items()
        },
        "bridge": {name: pin.model_dump(mode="json") for name, pin in closure["bridge"].items()},
        "policy_pins": {
            name: pin.model_dump(mode="json") for name, pin in closure["policy_pins"].items()
        },
        "qualification": {
            "policies": qualification["policies"],
            "reader": qualification["reader_json"],
            "custody": qualification["custody_json"],
            "rate_card_sha256": qualification["rate_card_sha256"],
            "runtime_control_pin": qualification["runtime_pin"].model_dump(mode="json"),
        },
        "services": {
            service: {
                "uid": service_uid[service],
                "principal": (
                    closure["pins"][service].service_principal_id
                    if service in closure["pins"]
                    else (
                        closure["driver_pins"][service].service_principal_id
                        if service in closure["driver_pins"]
                        else closure["capability_pin"].service_principal_id
                    )
                ),
                "receipt_key_path": keys["receipt"][service]["path"],
                "receipt_key_id": keys["receipt"][service]["key_id"],
                "service_id": entry["service_id"],
                "service_pin_sha256": entry["service_pin_sha256"],
                "socket_path": entry["socket_path"],
                "identity_manifest_sha256": entry["identity_manifest_sha256"],
                "config_path": entry["config_path"],
                "config_file_sha256": entry["config_file_sha256"],
                "deployment_path": entry["deployment_path"],
                "deployment_file_sha256": entry["deployment_file_sha256"],
                "deferred": service in DEFERRED_SERVICES,
            }
            for service, entry in services.items()
        },
        "deferred_services": list(DEFERRED_SERVICES),
        "request": {
            "request_id": request["request_id"],
            "request_path": request["request_path"],
            "request_sha256": request["request_sha256"],
        },
        "catalogs": closure["catalogs"],
        "offline_verification": state_extra,
    }
    _write_canonical(
        state_path,
        canonical_json_bytes(state),
        mode=0o644,
        uid=args.driver_uid,
        gid=args.driver_gid,
    )


if __name__ == "__main__":
    raise SystemExit(main())
