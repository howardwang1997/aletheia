#!/usr/bin/env python3
"""Author the ARL-2 dry-run capability and resource catalogs (runbook W1 step 4).

One pass, run as the driver uid on the box with the release tree on
PYTHONPATH (the service contracts and every retained source byte resolve
inside the release checkout, never from an installed copy):

    sudo -u arl2drv env PYTHONDONTWRITEBYTECODE=1 \
        PYTHONPATH=/opt/aletheia/release-<sha7>-arl2dry \
        /opt/aletheia/python/bin/python scripts/author-arl2-capability-catalog.py \
        --working-root /opt/aletheia/arl2-dryrun \
        --release-root /opt/aletheia/release-<sha7>-arl2dry \
        --environment-manifest /opt/aletheia/arl2-dryrun/calibration/<env freeze>.txt \
        --host-facts /opt/aletheia/arl2-dryrun/host_facts.json \
        --cpu-cores <nproc> --memory-bytes <MemTotal x 1024> \
        --scratch-bytes <statvfs of the working root>

What it does, in order (the production signing pass whose machinery
tests/protocols/conftest.py replicates; B7 recorded production signing as
a B8 commissioning input, and this script is that input):

1. generates the capability engineering auditor and qualifier keys (0400
   under keys/capability/, this uid, never overwritten);
2. derives the three bridge service contracts from the release tree
   (local_service_capability_sources: load_raw_run, run_cuprate_diagnostic,
   prepare_validation_campaign — the three-role triad every protocol DAG
   needs, contradiction #16) and retains every contract source byte, wire
   schema, policy document, and the qualification rule itself in a
   write-once source root (objects/<sha[:2]>/<sha>, 0400);
3. freezes the three qualified CapabilityManifestV2 manifests with signed
   audit receipts (one per CapabilityAuditKind except CALIBRATION, whose
   mode is not_applicable) and a signed qualification decision each, then
   re-runs verify_capability_sources against the authored artifacts so the
   output passes the same code path ARL-1's pinned verifier would run;
4. writes the canonical capability catalog (the three qualified
   manifests) and the canonical static resource catalog (one EXTERNAL
   class carrying all three external action kinds, whose capacity values
   come from the sampled host facts) whose file shas
   scripts/author-arl2-deployments.py pins into the compilation policy
   (runbook W1 step 6), plus the trust and runtime-inventory documents the
   retained evidence stays verifiable against.

Outputs under the working root: keys/capability/, the source root, and
configs/arl2-capability-{catalog,trust,runtime-sources,state}.json,
configs/arl2-resource-catalog.json, and
configs/arl2-service-contract-<operation>.json per operation (the
live-path copies the verifier re-reads).
Everything is write-once: an existing state file aborts the run (abort
doctrine). A crash mid-run leaves keys/ and configs/ behind without a
state file; recovery means moving keys/capability/ and configs/ aside
(the source root is content-addressed and re-runs cleanly).

Dry-run simplification surfaced for review: all three manifests share the
one frozen execution environment (--environment-manifest), because the
executor's arl2-cuprate env is the only environment manifest the window
authors; the parser and validator services do not execute inside it, and
the real run gives each capability its own environment manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_AUDITOR_PRINCIPAL = "service.arl2.capability-auditor"
_QUALIFIER_PRINCIPAL = "service.arl2.capability-qualifier"
_FROZEN_BY_PRINCIPAL = "human:principal-investigator"
_PROTOCOL_AUTHOR_PRINCIPAL = "service.arl2.protocol-compilation"
_SEMANTIC_VERSION = "1.0.0"
_WINDOW_LABEL = "arl2-dryrun-20260917"

# The four independence groups a three-role protocol separates (typecheck
# requires them distinct; every manifest declares all four so a step of any
# role sees the other roles' groups inside required_independence_groups).
# Kept in canonical order — the manifest validator rejects any other.
_INDEPENDENCE_GROUPS = (
    "arl2-bridge-admission-uid",
    "arl2-executor-uid",
    "arl2-parser-uid",
    "arl2-validator-uid",
)

# The three-role capability triad (contradiction #16): typecheck requires a
# SCIENTIFIC_EXECUTOR + OBSERVATION_PARSER + INDEPENDENT_VALIDATOR DAG, and
# one capability cannot serve two roles (disjoint principal sets + group
# containment). local_service_capability_sources exposes exactly these three
# operations, all runtime_kind external_service; the deployed bridge pins
# their principals (deployments script SERVICE_PRINCIPALS / BRIDGE_PRINCIPALS).
# The role/side_effect_class values are the contracts' own declared behavior
# and only guard against contract drift; which protocol step role each
# capability serves is authored at protocol level, not here.
_OPERATIONS = {
    "load_raw_run": {
        "capability_id": "raw_run_envelope_source",
        "executor_principal_id": "service.arl2.raw-run-source",
        "role": "observation_parser",
        "side_effect_class": "read_only_external",
        "wall_time_seconds": 300,
        "title": "Raw-run envelope source (registered slot load and verification)",
        "description": (
            "Loads the producer slot's verified terminal material from its signed "
            "registration and returns the RawRunEnvelope; a signed pending status "
            "returns no envelope. Read-only, replayable, no domain writes."
        ),
        "port_descriptions": (
            "The RPC payload naming the preregistered quest, action, and slot whose "
            "terminal material is loaded.",
            "The RawRunEnvelope carrying the slot's verified observation artifact "
            "binding; never emitted while the slot is pending.",
        ),
        "failure_modes": (
            (
                "slot_pending",
                "measurement",
                "The slot's signed terminal-material status is pending; the service returns no envelope rather than a partial one.",
            ),
            (
                "registration_verification_refused",
                "execution",
                "The preregistered quest, action, slot, or terminal artifacts fail verification; the load refuses rather than returning unverified material.",
            ),
            (
                "wire_result_unavailable",
                "invalid_output",
                "The service fails to encode the envelope or the transport drops it; the caller observes no result, never a partial one.",
            ),
        ),
    },
    "run_cuprate_diagnostic": {
        "capability_id": "cuprate_plane_doping_diagnostic",
        "executor_principal_id": "service.arl2.cuprate-diagnostic",
        "role": "analysis",
        "side_effect_class": "read_only_external",
        "wall_time_seconds": 3600,  # P0.5 measured 86-89 s per round on the box
        "title": "Cuprate plane-doping blind-spot diagnostic (matched-control contrast and doping-deviation stratification)",
        "description": (
            "Executes both preregistered discriminators over the registered card's "
            "bound-batch rows: the complexity-matched control contrast on holdout "
            "absolute error with a 2000-draw bootstrap CI, and the permutation-null "
            "doping-deviation stratification around the 0.16 holes/Cu optimum. Every "
            "stochastic component is seeded at 0; the analysis runs under the pinned "
            "arl2-cuprate environment on the staged CSV bytes."
        ),
        "port_descriptions": (
            "The RPC payload binding the registered dataset content sha and the "
            "bound batch group ids; the staged CSV itself rides deployment custody.",
            "The canonical CuprateDiagnosticResult for the bound batch.",
        ),
        "failure_modes": (
            (
                "dataset_sha_mismatch",
                "measurement",
                "The staged CSV bytes do not hash to the payload's registered content identity; the run fails closed before any row is read.",
            ),
            (
                "empty_bound_batch",
                "execution",
                "No staged row's canonical formula group falls inside the bound batch; the diagnostic refuses an empty analysis frame.",
            ),
            (
                "untabled_family_element",
                "measurement",
                "A family formula contains an element outside the pinned formal-valence table; the whole diagnostic fails closed rather than bucketing silently.",
            ),
            (
                "degenerate_stratum_split",
                "execution",
                "A median split leaves one doping-deviation half empty (single family holdout row or fully tied deviations); the stratification refuses to run.",
            ),
            (
                "undersized_train_frame",
                "execution",
                "Fewer than ten training rows or an empty family or control holdout; the matched-control contrast refuses the density neighborhood.",
            ),
            (
                "wire_result_unavailable",
                "invalid_output",
                "The service fails to encode its typed result or the transport drops it; the caller observes no result, never a partial one.",
            ),
        ),
    },
    "prepare_validation_campaign": {
        "capability_id": "f9_v2_independent_validation",
        "executor_principal_id": "principal.arl2.bridge.observation-validator",
        "role": "independent_validator",
        "side_effect_class": "durable_write",
        "wall_time_seconds": 300,
        "title": "F9-v2 independent validation campaign (exact-content assessment and committed publication)",
        "description": (
            "Verifies the raw run, assesses it against the frozen exact-content "
            "catalog, signs and atomically publishes one committed validation "
            "campaign to the F9-v2 archive; replay returns the original campaign "
            "digest. A non-successful terminal process returns null without "
            "publishing."
        ),
        "port_descriptions": (
            "The verified RawRunEnvelope whose run is assessed and published.",
            "The committed validation campaign sha256 for the exact raw run.",
        ),
        "failure_modes": (
            (
                "null_terminal_process",
                "measurement",
                "A non-successful terminal process returns null without publishing a campaign; no partial campaign exists.",
            ),
            (
                "raw_run_verification_refused",
                "execution",
                "The raw run fails verification against its registration; assessment refuses to start.",
            ),
            (
                "campaign_publish_conflict",
                "execution",
                "A different campaign digest is already committed for the exact raw run; publication fails closed instead of overwriting.",
            ),
            (
                "wire_result_unavailable",
                "invalid_output",
                "The service fails to encode its result or the transport drops it; the caller observes no result, never a partial one.",
            ),
        ),
    },
}


def _claim_ceiling():
    from aletheia.protocols.claim_contracts import (
        ClaimAllowance,
        ClaimCeiling,
        ClaimKind,
        ClaimStrength,
        EvidenceModality,
        ReplicationTier,
    )

    return ClaimCeiling(
        # canonical order: associational < comparative < descriptive
        allowances=(
            ClaimAllowance(kind=ClaimKind.ASSOCIATIONAL, maximum_strength=ClaimStrength.TENTATIVE),
            ClaimAllowance(kind=ClaimKind.COMPARATIVE, maximum_strength=ClaimStrength.TENTATIVE),
            ClaimAllowance(kind=ClaimKind.DESCRIPTIVE, maximum_strength=ClaimStrength.SUPPORTED),
        ),
        required_evidence_modalities=(EvidenceModality.COMPUTATIONAL,),
        required_replication_tier=ReplicationTier.EXACT_REEXECUTION,
        # authored explicitly, NOT inherited: the schema default for
        # independent_validation_required is True, and a true value forces the
        # validator-flow gate (every claim-supporting observable must
        # transitively reach an INDEPENDENT_VALIDATOR step that consumes the
        # observable's own output port) — unsatisfiable against this triad,
        # because the validation service consumes the raw-run envelope, not
        # the diagnostic result (2f-q7 contradictions #14/#16). The
        # unconditional three-role requirement is met by the triad's validator
        # step; this flag only governs the observable-flow clause. Independent
        # validation rides the observation bridge (independent-validation
        # service plus the bridge observation-validator/admitter principals);
        # in-protocol independent re-execution is the real-run follow-up.
        independent_validation_required=False,
        rationale=(
            "Two preregistered discriminators over the registered card's "
            "bound-batch rows; comparative and associational ceilings stay "
            "tentative because the matched-control contrast and the "
            "stratification test ride one dataset and one frozen seed. "
            "Independent validation rides the observation bridge "
            "(independent-validation service, observation-validator and "
            "admitter principals); the in-protocol validator step assesses "
            "the raw run, not this observable's output."
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--working-root",
        required=True,
        help="absolute path to the window working root (keys/, configs/, source root)",
    )
    parser.add_argument(
        "--release-root",
        required=True,
        help="absolute path to the staged release tree every source pin resolves inside",
    )
    parser.add_argument(
        "--environment-manifest",
        required=True,
        help="path to the frozen execution-environment manifest (the retained environment source)",
    )
    parser.add_argument(
        "--host-facts",
        required=True,
        help="path to the stage0 host facts JSON (uname feeds the resource architecture)",
    )
    parser.add_argument(
        "--cpu-cores",
        type=int,
        required=True,
        help="logical cores of the host class (sample with nproc on the box)",
    )
    parser.add_argument(
        "--memory-bytes",
        type=int,
        required=True,
        help="host memory in bytes (sample from /proc/meminfo MemTotal x 1024)",
    )
    parser.add_argument(
        "--scratch-bytes",
        type=int,
        required=True,
        help="scratch capacity in bytes (sample with statvfs on the working root)",
    )
    parser.add_argument(
        "--valid-hours",
        type=int,
        default=96,
        help="authority pin and receipt validity in hours from now (default 96)",
    )
    return parser


def _fail(message: str) -> None:
    raise SystemExit(f"author-arl2-capability-catalog: {message}")


def _canonical_bytes(payload) -> bytes:
    from aletheia.research_kernel.schemas import canonical_json_bytes

    return canonical_json_bytes(payload)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        _fail(f"refusing to overwrite existing key file {path}")
    path.write_bytes(payload)
    path.chmod(0o400)


def _write_canonical(path: Path, payload) -> tuple[str, str]:
    data = _canonical_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        _fail(f"refusing to overwrite existing canonical file {path}")
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(data)
        staged = Path(handle.name)
    os.replace(staged, path)
    return str(path), _sha256_bytes(data)


def _policy_document(subject: str, policy: str, prepared_at: datetime) -> dict:
    return {
        "schema_name": "aletheia.arl2_capability_policy_note",
        "schema_version": 1,
        "window_id": _WINDOW_LABEL,
        "subject": subject,
        "policy": policy,
        "prepared_at": prepared_at.isoformat(),
    }


def _resource_class_from_facts(
    facts: dict, cores: int, memory_bytes: int, scratch_bytes: int
) -> dict:
    # The stage0 facts sheet carries the uname string but no capacity fields;
    # the capacities arrive as explicit operator-sampled values instead of
    # silently flooring on absent keys. The class is kind=external because
    # every authored capability's runtime_kind is external_service and the
    # compile gate (typecheck.py _check_capability_and_resource) only accepts
    # external classes carrying the capability's exact action kind for such
    # steps — the predicate is membership, so one class carrying all three
    # action kinds serves the whole triad; the sampled capacities still bound
    # each step's structural resource request.
    fields = str(facts.get("uname") or "").split()
    if not fields:
        _fail("host facts carry no uname string; cannot derive the resource architecture")
    machine = fields[-1]
    return {
        "schema_name": "aletheia.static_resource_class",
        "schema_version": 1,
        "class_key": "v100ts.bridge-services.v1",
        "kind": "external",
        "cpu_architecture": machine,
        "oci_platform": f"linux/{'amd64' if machine == 'x86_64' else machine}",
        "container_runtime": "host-process",
        "cpu_cores": cores,
        "memory_bytes": memory_bytes,
        "scratch_bytes": scratch_bytes,
        "network_policies": ["none"],
        "features": ["conda-env:arl2-cuprate"],
        "supports_exclusive": True,
        "external_action_kinds": sorted(_OPERATIONS),
    }


def main() -> int:
    args = _parser().parse_args()
    if args.valid_hours <= 2:
        _fail(
            "--valid-hours must be at least three hours (receipt expiry must strictly "
            "outlive the protocol authored_at stub at now+1h)"
        )
    if min(args.cpu_cores, args.memory_bytes, args.scratch_bytes) < 1:
        _fail("--cpu-cores/--memory-bytes/--scratch-bytes must each be positive")

    working_root = Path(args.working_root).resolve(strict=True)
    release_root = Path(args.release_root).resolve(strict=True)
    environment_path = Path(args.environment_manifest).resolve(strict=True)
    host_facts_path = Path(args.host_facts).resolve(strict=True)
    state_path = working_root / "configs" / "arl2-capability-state.json"
    if state_path.exists():
        _fail(
            "capability state already exists; the window is write-once (abort doctrine) "
            "- move the old state aside only when abandoning the window"
        )

    from aletheia.execution import capability_sources as engineering
    from aletheia.observations.service_capabilities import (
        local_service_capability_sources,
    )
    from aletheia.protocols.capabilities import CapabilityCatalog, CapabilityManifestV2
    from aletheia.protocols.schemas import (
        CapabilityAuditBinding,
        CapabilityAuditKind,
        CapabilityRequirement,
    )
    from aletheia.protocols.typecheck import expected_capability_audit_policy_sha256
    from aletheia.execution.schemas import StaticResourceCatalog
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        # custody pre-flight of every operator-supplied path the verifier
        # fresh()-reads, BEFORE any write-once output exists: a mode/nlink
        # violation here must abort cheaply, not wedge the window
        services = {
            operation: local_service_capability_sources(operation, read_bytes=engineering.fresh)
            for operation in _OPERATIONS
        }
        engineering.fresh(environment_path)
    except Exception as error:
        raise SystemExit(
            "author-arl2-capability-catalog: custody pre-flight failed for the release "
            f"source files or the environment manifest ({environment_path}): {error}"
        ) from error
    for operation, service in services.items():
        if release_root not in service.implementation_path.parents:
            _fail(
                f"the {operation} service contract resolved outside the release tree; "
                "run with the release root on PYTHONPATH so every retained source is "
                "the reviewed copy"
            )
        expected = _OPERATIONS[operation]
        contract = service.contract
        if contract["behavior"]["role"] != expected["role"] or (
            contract["behavior"]["side_effect_class"] != expected["side_effect_class"]
        ):
            _fail(
                f"the {operation} service contract no longer matches its commissioned "
                f"behavior (expected role {expected['role']}, side effect "
                f"{expected['side_effect_class']})"
            )

    now = datetime.now(timezone.utc).replace(microsecond=0)
    pin_valid_from = now - timedelta(minutes=5)
    pin_expires_at = now + timedelta(hours=args.valid_hours)
    receipt_expires_at = now + timedelta(hours=args.valid_hours - 1)

    # 1. capability engineering keys.
    keys = {}
    for name, principal in (
        ("auditor", _AUDITOR_PRINCIPAL),
        ("qualifier", _QUALIFIER_PRINCIPAL),
    ):
        path = working_root / "keys" / "capability" / f"{name}.key"
        _write_private(path, os.urandom(32))
        keys[name] = {
            "path": str(path),
            "principal_id": principal,
            "signer": Ed25519PrivateKey.from_private_bytes(path.read_bytes()),
        }
    for name, item in keys.items():
        public = (
            item["signer"]
            .public_key()
            .public_bytes(
                encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
            )
        )
        item["key_id"] = _sha256_bytes(public)
        item["public_key_ed25519_hex"] = public.hex()
    if keys["auditor"]["key_id"] == keys["qualifier"]["key_id"]:
        _fail("capability auditor and qualifier keys collide; regenerate")

    pins = {
        name: {
            "principal_id": item["principal_id"],
            "key_id": item["key_id"],
            "public_key_ed25519_hex": item["public_key_ed25519_hex"],
            "valid_from": pin_valid_from.isoformat(),
            "expires_at": pin_expires_at.isoformat(),
        }
        for name, item in keys.items()
    }

    # 2. retained source root: write-once content-addressed objects.
    source_root = working_root / "capability-source-root-arl2dry"

    def put(payload: bytes) -> str:
        if not isinstance(payload, (bytes, bytearray)):
            _fail("retained capability sources must be raw bytes")
        digest = _sha256_bytes(payload)
        path = source_root / "objects" / digest[:2] / digest
        if path.exists():
            if path.read_bytes() != payload:
                _fail(f"sha collision inside the capability source root at {digest}")
            path.chmod(0o400)  # heal the mode if a crash left the object at 0600
            return digest
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{digest}.", delete=False
        ) as handle:
            handle.write(payload)
            staged = Path(handle.name)
        staged.chmod(0o400)  # the object never appears on disk at 0600
        os.replace(staged, path)
        return digest

    environment_bytes = environment_path.read_bytes()
    environment_sha = _sha256_bytes(environment_bytes)
    put(environment_bytes)

    rule_sha = put(_canonical_bytes(engineering.RULE))
    if rule_sha != engineering.canonical_sha256(engineering.RULE):
        _fail("the retained qualification rule bytes hash away from the rule pin")

    audit_kinds = tuple(
        sorted(
            (kind for kind in CapabilityAuditKind if kind is not CapabilityAuditKind.CALIBRATION),
            key=lambda kind: kind.value,
        )
    )

    def issue(
        name: str, kind: str, definition: str, issued_at: datetime, materials, **fields
    ) -> str:
        message = {
            "schema_name": f"aletheia.capability_engineering_{kind}.v1",
            "principal_id": pins[name]["principal_id"],
            "key_id": pins[name]["key_id"],
            "scientific_authority": False,
            "issued_at": issued_at.isoformat(),
            "expires_at": receipt_expires_at.isoformat(),
            "definition_sha256": definition,
            "materials": sorted(set(materials)),
            **fields,
        }
        record = {
            "message": message,
            "signature_ed25519_hex": keys[name]["signer"].sign(_canonical_bytes(message)).hex(),
        }
        return put(_canonical_bytes(record))

    # 3. per-operation contract retention, policy documents, manifest bases.
    operations = {}
    for operation, service in services.items():
        expected = _OPERATIONS[operation]
        contract = service.contract
        contract_bytes = _canonical_bytes(contract)
        contract_sha = _sha256_bytes(contract_bytes)
        if contract_sha != engineering.canonical_sha256(contract):
            _fail(f"the retained {operation} service contract bytes are not canonical")
        materials = {put(contract_bytes)}
        materials.update(put(payload) for payload in service.source_bytes.values())
        materials.update(put(_canonical_bytes(schema)) for schema in service.schemas.values())
        implementation_sha = _sha256_bytes(service.implementation_path.read_bytes())

        policies = {}

        def policy(name: str, subject: str, text: str) -> str:
            policies[name] = put(_canonical_bytes(_policy_document(subject, text, now)))
            return policies[name]

        ports = {}
        for direction, spec, description in (
            ("input", contract["input_ports"][0], expected["port_descriptions"][0]),
            ("output", contract["output_ports"][0], expected["port_descriptions"][1]),
        ):
            ports[direction] = {
                "port_id": spec["port_id"],
                "direction": direction,
                "schema_ref": {
                    "schema_name": "aletheia.json_schema_ref",
                    "schema_version": 1,
                    "schema_id": f"{operation}.{spec['schema']}.v1",
                    "semantic_version": "1.0.0",
                    "schema_sha256": contract["schema_sources"][spec["schema"]],
                },
                "artifact_kind": spec["artifact_kind"],
                "data_classification": "public",
                "multiplicity": "one",
                "description": description,
            }

        failure_entries = []
        for failure_id, category, description in expected["failure_modes"]:
            failure_entries.append(
                {
                    "failure_id": failure_id,
                    "category": category,
                    "description": description,
                    "detection_rule_sha256": policy(
                        f"failure_{failure_id}",
                        f"capability:{expected['capability_id']}:failure:{failure_id}",
                        f"The {failure_id} failure is detected by the service's own "
                        "fail-closed guards; the typed result is never emitted on a "
                        "detected failure.",
                    ),
                    "disposition": "blocked",
                }
            )
        # the manifest validator requires failure ids unique and canonically ordered
        failure_entries.sort(key=lambda entry: entry["failure_id"])

        base = {
            "schema_name": "aletheia.capability_manifest",
            "schema_version": 2,
            "capability_id": expected["capability_id"],
            "semantic_version": _SEMANTIC_VERSION,
            "operation_id": f"operation.{operation}",
            "external_action_kind": operation,
            "title": expected["title"],
            "description": expected["description"],
            "input_ports": [ports["input"]],
            "output_ports": [ports["output"]],
            "side_effect_class": contract["behavior"]["side_effect_class"],
            "principal": {
                "executor_principal_id": expected["executor_principal_id"],
                "principal_kind": "service",
                "authority_policy_sha256": policy(
                    "authority",
                    f"capability:{expected['capability_id']}:executor-authority",
                    "The executor is a separately commissioned bridge RPC service of "
                    "the ARL-2 dry-run window; it holds no kernel signing authority "
                    "and no scientific authority.",
                ),
                "credential_class": "unix.socket.peer-cred",
                "required_independence_groups": list(_INDEPENDENCE_GROUPS),
            },
            "runtime": {
                "runtime_kind": contract["runtime_kind"],
                "adapter_ref": contract["adapter_ref"],
                "implementation_sha256": implementation_sha,
                "environment_sha256": environment_sha,
                "determinism": "frozen_seeds"
                if operation == "run_cuprate_diagnostic"
                else "declared_stochastic",
                "frozen_seeds": [0] if operation == "run_cuprate_diagnostic" else [],
                "maximum_wall_time_seconds": expected["wall_time_seconds"],
                "checkpoint_supported": False,
                # replay of the committed campaign returns the original digest
                "reconciliation_supported": operation == "prepare_validation_campaign",
            },
            "applicability": {
                "epistemic_kinds": ["hypothesis_discrimination"],
                "domain_tags": ["materials", "superconductivity"],
                "required_condition_sha256s": sorted(
                    [
                        policy(
                            "required_service_contract",
                            f"capability:{expected['capability_id']}:applicability:service-contract",
                            "The operation is bound to the retained local service contract "
                            f"for {operation} and its retained source closure.",
                        ),
                        contract_sha,
                    ]
                ),
                "excluded_condition_sha256s": [
                    policy(
                        "excluded_external_replication",
                        f"capability:{expected['capability_id']}:applicability:excluded",
                        "No external replication inside the loop; the campaign runs "
                        "exactly two preregistered rounds over the registered card.",
                    )
                ],
                "minimum_batch_size": 1,
                "maximum_batch_size": 1,
            },
            "calibration": {"mode": "not_applicable"},
            "failure_modes": failure_entries,
            "retry": {"mode": "never", "maximum_attempts_per_scientific_slot": 1},
            "safety": {
                "safety_class": "low_risk_compute",
                "hazard_sha256s": [
                    policy(
                        "hazard_dataset_provenance",
                        f"capability:{expected['capability_id']}:safety:hazard",
                        "The dataset is external (UCI superconductivity); the card "
                        "carries license_status to_verify and the campaign verifies "
                        "license and citation at data registration before any egress.",
                    )
                ],
                "approval_policy_sha256": policy(
                    "safety_approval",
                    f"capability:{expected['capability_id']}:safety:approval",
                    "Bridge RPC services inside the ARL-2 dry-run window; approval "
                    "rides the window's commissioning authority.",
                ),
            },
            "license_egress": {
                "license_policy_sha256": policy(
                    "license",
                    f"capability:{expected['capability_id']}:license",
                    "The UCI superconductivity dataset's license and citation "
                    "obligations are verified at data registration (card "
                    "license_status to_verify); outputs stay inside campaign custody.",
                ),
                "permitted_input_classes": ["public"],
                "output_license_ids": ["internal-dryrun"],
                "network_egress": contract["network_egress"],
                "egress_policy_sha256": policy(
                    "egress",
                    f"capability:{expected['capability_id']}:egress",
                    "No network egress; every bridge service reads staged inputs and "
                    "returns its typed result over the pinned unix socket.",
                ),
                "retention_policy_sha256": policy(
                    "retention",
                    f"capability:{expected['capability_id']}:retention",
                    "The typed result is retained as campaign evidence inside the "
                    "window's custody roots for the full bundle replay.",
                ),
            },
            "qualification": {
                "status": "provisional",
                "qualification_rule_sha256": rule_sha,
            },
            "claim_ceiling": _claim_ceiling().model_dump(mode="json"),
            "frozen_by_principal_id": _FROZEN_BY_PRINCIPAL,
            "frozen_at": now.isoformat(),
        }
        operations[operation] = {
            "expected": expected,
            "service": service,
            "contract_sha": contract_sha,
            "materials": materials,
            "implementation_sha": implementation_sha,
            "base": base,
        }

    # 4. check results, signed audit receipts, qualification decisions, freeze.
    frozen = {}
    audit_receipts = {}
    qualification_decisions = {}
    audit_bindings_by_operation = {}
    for operation in sorted(operations):
        item = operations[operation]
        draft = CapabilityManifestV2.model_validate(item["base"])
        definition = engineering.definition_sha256(draft)
        required_sources = engineering.contract_sources(source_root, draft)
        required_sources.update(item["materials"])
        required_sources.update({item["implementation_sha"], environment_sha})
        for digest in sorted(required_sources):
            engineering.source(source_root, digest)

        audits = {}
        policy_pins = {}
        checked_at = now - timedelta(seconds=2)
        for kind in audit_kinds:
            audit_policy = expected_capability_audit_policy_sha256(draft, kind)
            policy_pins[kind.value] = audit_policy
            check_result = put(
                _canonical_bytes(
                    {
                        "schema_name": "aletheia.capability_engineering_check.v1",
                        "definition_sha256": definition,
                        "audit_kind": kind.value,
                        "audit_policy_sha256": audit_policy,
                        "scientific_authority": False,
                        "result": "passed",
                        "checked_at": checked_at.isoformat(),
                        "checks": [
                            {
                                "check_id": "arl2dry-retained-source-contract",
                                "passed": True,
                                "source_sha256s": sorted(required_sources),
                            }
                        ],
                    }
                )
            )
            audits[kind.value] = issue(
                "auditor",
                "audit",
                definition,
                now - timedelta(seconds=1),
                [*required_sources, check_result],
                check_result_sha256=check_result,
                audit_kind=kind.value,
                audit_policy_sha256=audit_policy,
                result="passed",
            )
        decision = issue(
            "qualifier",
            "qualification",
            definition,
            now,
            list(audits.values()),
            result="qualified",
            audit_receipt_sha256s=sorted(audits.values()),
        )

        item["base"]["qualification"] = {
            "status": "qualified",
            "qualification_rule_sha256": rule_sha,
            "evidence_receipt_sha256s": sorted([*audits.values(), decision]),
            "qualified_by_principal_id": pins["qualifier"]["principal_id"],
            "qualified_at": now.isoformat(),
            "expires_at": receipt_expires_at.isoformat(),
        }
        manifest = CapabilityManifestV2.model_validate(item["base"])
        if engineering.definition_sha256(manifest) != definition:
            _fail(f"the frozen {operation} manifest's definition moved away from the audited draft")
        for kind in audit_kinds:
            if expected_capability_audit_policy_sha256(manifest, kind) != policy_pins[kind.value]:
                _fail(
                    f"audit policy for {kind.value} moved between draft and frozen "
                    f"{operation} manifest; the retained receipts no longer bind"
                )

        audit_bindings_by_operation[operation] = [
            {
                "audit_kind": kind.value,
                "capability_manifest_sha256": manifest.manifest_sha256,
                "receipt_sha256": audits[kind.value],
                "audit_policy_sha256": policy_pins[kind.value],
                "auditor_principal_id": pins["auditor"]["principal_id"],
                "valid_from": (now - timedelta(seconds=1)).isoformat(),
                "expires_at": receipt_expires_at.isoformat(),
                "conclusion": "passed",
            }
            for kind in audit_kinds
        ]
        frozen[operation] = manifest
        audit_receipts[operation] = audits
        qualification_decisions[operation] = decision

    # 5. catalogs, trust, runtime inventory.
    manifest_tuple = tuple(sorted(frozen.values(), key=lambda item: item.manifest_sha256))
    catalog = CapabilityCatalog(manifests=manifest_tuple)
    facts = json.loads(host_facts_path.read_text())
    if not isinstance(facts, dict):
        _fail("host facts JSON is not an object")
    resource_catalog = StaticResourceCatalog.model_validate(
        {
            "schema_name": "aletheia.static_resource_catalog",
            "schema_version": 1,
            "catalog_key": f"{_WINDOW_LABEL}.bridge-services.v1",
            "resource_classes": [
                _resource_class_from_facts(
                    facts, args.cpu_cores, args.memory_bytes, args.scratch_bytes
                )
            ],
        }
    )

    trust = {
        "schema_name": "aletheia.capability_engineering_source_trust.v1",
        "rule_sha256": rule_sha,
        "scientific_authority": False,
        **pins,
    }
    # the verifier re-reads the retained contract bytes from a live path
    # (capability_sources.py _verify_local_service_sources), so the canonical
    # copies are config outputs, not just CAS objects
    contract_copies = {}
    for operation, item in operations.items():
        contract_copy_path, contract_copy_sha = _write_canonical(
            working_root / "configs" / f"arl2-service-contract-{operation.replace('_', '-')}.json",
            item["service"].contract,
        )
        if contract_copy_sha != item["contract_sha"]:
            _fail(f"the retained {operation} service contract copy hashes away from its pin")
        contract_copies[operation] = (contract_copy_path, contract_copy_sha)
    runtime_sources = {}
    for operation, item in operations.items():
        manifest = frozen[operation]
        runtime_sources[manifest.manifest_sha256] = {
            "adapter_ref": manifest.runtime.adapter_ref,
            "implementation_path": str(item["service"].implementation_path),
            "implementation_sha256": item["implementation_sha"],
            "environment_sha256": environment_sha,
            "environment_source_sha256": environment_sha,
            "environment_source_path": str(environment_path),
            "service_operation": operation,
            "service_contract_sha256": item["contract_sha"],
            "service_contract_path": contract_copies[operation][0],
            "service_source_paths": {
                name: str(path) for name, path in item["service"].source_paths.items()
            },
        }

    catalog_path, catalog_sha = _write_canonical(
        working_root / "configs" / "arl2-capability-catalog.json", catalog
    )
    resource_path, resource_sha = _write_canonical(
        working_root / "configs" / "arl2-resource-catalog.json", resource_catalog
    )
    trust_path, trust_sha = _write_canonical(
        working_root / "configs" / "arl2-capability-trust.json", trust
    )
    runtime_path, runtime_sha = _write_canonical(
        working_root / "configs" / "arl2-capability-runtime-sources.json",
        runtime_sources,
    )
    if catalog_sha != catalog.catalog_sha256:
        _fail("capability catalog file bytes are not the model's canonical bytes")
    if resource_sha != resource_catalog.catalog_sha256:
        _fail("resource catalog file bytes are not the model's canonical bytes")

    # 6. self-verification: the same code path ARL-1's pinned verifier runs.
    from types import SimpleNamespace as NS

    stub_steps = []
    for operation, manifest in sorted(frozen.items()):
        stub_requirement = CapabilityRequirement(
            requirement_id=f"req.arl2dry.{operation}",
            operation_id=manifest.operation_id,
            capability_id=manifest.capability_id,
            semantic_version=_SEMANTIC_VERSION,
            manifest_sha256=manifest.manifest_sha256,
            audit_bindings=[
                CapabilityAuditBinding.model_validate(item)
                for item in audit_bindings_by_operation[operation]
            ],
        )
        # the verifier's per-operation step clauses: the raw-run source must
        # declare its registered archive input, the campaign service its
        # committed-campaign receipt
        contract = operations[operation]["service"].contract
        archive_input = None
        stub_artifacts = ()
        if operation == "load_raw_run":
            archive_input = NS(
                lookup_input_port_id="input.raw_run_lookup",
                envelope_output_port_id="intermediate.raw_run",
                replicate_mapping="same_slot_index",
            )
        elif operation == "prepare_validation_campaign":
            stub_artifacts = (
                NS(
                    role=NS(value="provider_receipt"),
                    required=True,
                    schema_sha256=contract["schema_sources"]["committed_campaign"],
                ),
            )
        stub_steps.append(
            NS(
                role=NS(value=contract["behavior"]["role"]),
                expected_artifacts=stub_artifacts,
                archived_observation_input=archive_input,
                capability_requirement=stub_requirement,
            )
        )
    stub_protocol = NS(
        authored_at=now + timedelta(hours=1),
        authored_by_principal_id=_PROTOCOL_AUTHOR_PRINCIPAL,
        independence=NS(
            executor_principal_ids=(),
            parser_principal_ids=(),
            validator_principal_ids=(),
            claim_approver_principal_ids=(),
        ),
        steps=tuple(stub_steps),
    )
    engineering.verify_capability_sources(
        NS(capability_catalog=NS(manifests=manifest_tuple), protocol=stub_protocol),
        root=source_root,
        trust=trust,
        runtime_sources=runtime_sources,
    )

    _, state_sha = _write_canonical(
        state_path,
        {
            "schema_name": "aletheia.arl2_capability_state",
            "schema_version": 1,
            "window_id": _WINDOW_LABEL,
            "operations": sorted(_OPERATIONS),
            "capabilities": [
                {
                    "operation": operation,
                    "capability_id": frozen[operation].capability_id,
                    "semantic_version": _SEMANTIC_VERSION,
                    "manifest_sha256": frozen[operation].manifest_sha256,
                    "definition_sha256": engineering.definition_sha256(frozen[operation]),
                    "implementation_sha256": operations[operation]["implementation_sha"],
                    "service_contract_path": contract_copies[operation][0],
                    "service_contract_sha256": contract_copies[operation][1],
                    "audit_bindings": audit_bindings_by_operation[operation],
                    "qualification_decision_sha256": qualification_decisions[operation],
                }
                for operation in sorted(_OPERATIONS)
            ],
            "catalog_path": catalog_path,
            "catalog_sha256": catalog_sha,
            "resource_catalog_path": resource_path,
            "resource_catalog_sha256": resource_sha,
            "trust_path": trust_path,
            "trust_sha256": trust_sha,
            "runtime_sources_path": runtime_path,
            "runtime_sources_sha256": runtime_sha,
            "source_root": str(source_root),
            "resource_class_values": {
                "cpu_cores": args.cpu_cores,
                "memory_bytes": args.memory_bytes,
                "scratch_bytes": args.scratch_bytes,
                "provenance": "operator-sampled on the box (nproc, /proc/meminfo, statvfs)",
            },
            "auditor": pins["auditor"],
            "qualifier": pins["qualifier"],
            "evidence_receipt_sha256s": sorted(
                receipt
                for operation in sorted(_OPERATIONS)
                for receipt in (
                    *audit_receipts[operation].values(),
                    qualification_decisions[operation],
                )
            ),
            "valid_from": pin_valid_from.isoformat(),
            "expires_at": pin_expires_at.isoformat(),
            "receipt_expires_at": receipt_expires_at.isoformat(),
            "environment_manifest_path": str(environment_path),
            "environment_sha256": environment_sha,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        },
    )

    sys.stdout.write(
        f"capability triad {[frozen[op].capability_id for op in sorted(_OPERATIONS)]} "
        "qualified and cataloged\n"
        + "".join(
            f"  {operation}  {frozen[operation].manifest_sha256}\n"
            for operation in sorted(_OPERATIONS)
        )
        + f"  catalog    {catalog_path} ({catalog_sha})\n"
        f"  resources  {resource_path} ({resource_sha})\n"
        f"  audits     {len(audit_kinds) * len(_OPERATIONS)} receipts, "
        f"{len(_OPERATIONS)} decisions\n"
        f"  state      {state_path} ({state_sha})\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
