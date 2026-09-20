#!/usr/bin/env python3
"""Author the ARL-2 dry-run capability and resource catalogs (runbook W1 step 4).

One pass, run as the driver uid on the box with the release tree on
PYTHONPATH (the service contract and every retained source byte resolve
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
2. derives the cuprate diagnostic service contract from the release tree
   (local_service_capability_sources) and retains every contract source
   byte, wire schema, policy document, and the qualification rule itself
   in a write-once source root (objects/<sha[:2]>/<sha>, 0400);
3. freezes the qualified CapabilityManifestV2 for run_cuprate_diagnostic
   with signed audit receipts (one per CapabilityAuditKind except
   CALIBRATION, whose mode is not_applicable) and a signed qualification
   decision, then re-runs verify_capability_sources against the authored
   artifacts so the output passes the same code path ARL-1's pinned
   verifier would run;
4. writes the canonical capability catalog (the single qualified
   manifest) and the canonical static resource catalog (one external
   class serving the authored capability's action kind, whose capacity
   values come from the sampled host facts) whose file
   shas scripts/author-arl2-deployments.py pins into the compilation
   policy (runbook W1 step 6), plus the trust and runtime-inventory
   documents the retained evidence stays verifiable against.

Outputs under the working root: keys/capability/, the source root, and
configs/arl2-capability-{catalog,trust,runtime-sources,state}.json,
configs/arl2-resource-catalog.json, and configs/arl2-cuprate-service-contract.json
(the live-path copy the verifier re-reads).
Everything is write-once: an existing state file aborts the run (abort
doctrine). A crash mid-run leaves keys/ and configs/ behind without a
state file; recovery means moving keys/capability/ and configs/ aside
(the source root is content-addressed and re-runs cleanly).
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
_EXECUTOR_PRINCIPAL = "service.arl2.cuprate-diagnostic"
_PROTOCOL_AUTHOR_PRINCIPAL = "service.arl2.protocol-compilation"
_CAPABILITY_ID = "cuprate_plane_doping_diagnostic"
_SEMANTIC_VERSION = "1.0.0"
_OPERATION = "run_cuprate_diagnostic"
_WINDOW_LABEL = "arl2-dryrun-20260917"
_MAX_WALL_TIME_SECONDS = 3600  # P0.5 measured 86-89 s per round on the box

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
        # independent_validation_required is True, and a true value forces an
        # INDEPENDENT_VALIDATOR step that consumes the observable's output
        # port — unsatisfiable against a single-capability catalog, because a
        # step port must exist in its capability's interface and no second
        # capability can carry the diagnostic output as an input (2f-q7
        # contradiction #14). This dry run's independent validation rides the
        # observation bridge (independent-validation service plus the bridge
        # observation-validator/admitter principals); in-protocol independent
        # re-execution is the real-run follow-up.
        independent_validation_required=False,
        rationale=(
            "Two preregistered discriminators over the registered card's "
            "bound-batch rows; comparative and associational ceilings stay "
            "tentative because the matched-control contrast and the "
            "stratification test ride one dataset and one frozen seed. "
            "Independent validation rides the observation bridge "
            "(independent-validation service, observation-validator and "
            "admitter principals), not an in-protocol validator step."
        ),
    )


# (failure_id, category, description): the fail-closed guards B7 added to
# the diagnostic itself; every one refuses the typed result outright.
_FAILURE_MODES = (
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
    facts: dict, cores: int, memory_bytes: int, scratch_bytes: int, action_kind: str
) -> dict:
    # The stage0 facts sheet carries the uname string but no capacity fields;
    # the capacities arrive as explicit operator-sampled values instead of
    # silently flooring on absent keys. The class is kind=external because the
    # authored capability's runtime_kind is external_service and the compile
    # gate (typecheck.py _check_capability_and_resource) only accepts external
    # classes carrying the capability's exact action kind for such steps; the
    # sampled capacities still bound the step's structural resource request.
    fields = str(facts.get("uname") or "").split()
    if not fields:
        _fail("host facts carry no uname string; cannot derive the resource architecture")
    machine = fields[-1]
    return {
        "schema_name": "aletheia.static_resource_class",
        "schema_version": 1,
        "class_key": "v100ts.cuprate-diagnostic.v1",
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
        "external_action_kinds": [action_kind],
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
        service = local_service_capability_sources(_OPERATION, read_bytes=engineering.fresh)
        engineering.fresh(environment_path)
    except Exception as error:
        raise SystemExit(
            "author-arl2-capability-catalog: custody pre-flight failed for the release "
            f"source files or the environment manifest ({environment_path}): {error}"
        ) from error
    if release_root not in service.implementation_path.parents:
        _fail(
            "the service contract resolved outside the release tree; run with the "
            "release root on PYTHONPATH so every retained source is the reviewed copy"
        )
    contract = service.contract
    if contract["behavior"]["role"] != "analysis" or (
        contract["behavior"]["side_effect_class"] != "read_only_external"
    ):
        _fail("the cuprate service contract is no longer a read-only analysis operation")

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
        public = item["signer"].public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
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

    contract_bytes = _canonical_bytes(contract)
    contract_sha = _sha256_bytes(contract_bytes)
    if contract_sha != engineering.canonical_sha256(contract):
        _fail("the retained service contract bytes are not canonical")

    rule_sha = put(_canonical_bytes(engineering.RULE))
    service_materials = {put(contract_bytes)}
    service_materials.update(put(payload) for payload in service.source_bytes.values())
    service_materials.update(
        put(_canonical_bytes(schema)) for schema in service.schemas.values()
    )
    implementation_sha = _sha256_bytes(service.implementation_path.read_bytes())
    environment_bytes = environment_path.read_bytes()
    environment_sha = _sha256_bytes(environment_bytes)
    put(environment_bytes)

    # 3. policy documents the manifest pins by sha.
    policies = {}

    def policy(name: str, subject: str, text: str) -> str:
        policies[name] = put(_canonical_bytes(_policy_document(subject, text, now)))
        return policies[name]

    ports = {}
    for direction, spec, description in (
        (
            "input",
            contract["input_ports"][0],
            "The RPC payload binding the registered dataset content sha and the "
            "bound batch group ids; the staged CSV itself rides deployment custody.",
        ),
        (
            "output",
            contract["output_ports"][0],
            "The canonical CuprateDiagnosticResult for the bound batch.",
        ),
    ):
        ports[direction] = {
            "port_id": spec["port_id"],
            "direction": direction,
            "schema_ref": {
                "schema_name": "aletheia.json_schema_ref",
                "schema_version": 1,
                "schema_id": f"cuprate-diagnostic.{spec['schema']}.v1",
                "semantic_version": "1.0.0",
                "schema_sha256": contract["schema_sources"][spec["schema"]],
            },
            "artifact_kind": spec["artifact_kind"],
            "data_classification": "public",
            "multiplicity": "one",
            "description": description,
        }

    failure_entries = []
    for failure_id, category, description in _FAILURE_MODES:
        failure_entries.append(
            {
                "failure_id": failure_id,
                "category": category,
                "description": description,
                "detection_rule_sha256": policy(
                    f"failure_{failure_id}",
                    f"capability:failure:{failure_id}",
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
        "capability_id": _CAPABILITY_ID,
        "semantic_version": _SEMANTIC_VERSION,
        "operation_id": f"operation.{_OPERATION}",
        "external_action_kind": _OPERATION,
        "title": "Cuprate plane-doping blind-spot diagnostic (matched-control contrast and doping-deviation stratification)",
        "description": (
            "Executes both preregistered discriminators over the registered card's "
            "bound-batch rows: the complexity-matched control contrast on holdout "
            "absolute error with a 2000-draw bootstrap CI, and the permutation-null "
            "doping-deviation stratification around the 0.16 holes/Cu optimum. Every "
            "stochastic component is seeded at 0; the analysis runs under the pinned "
            "arl2-cuprate environment on the staged CSV bytes."
        ),
        "input_ports": [ports["input"]],
        "output_ports": [ports["output"]],
        "side_effect_class": contract["behavior"]["side_effect_class"],
        "principal": {
            "executor_principal_id": _EXECUTOR_PRINCIPAL,
            "principal_kind": "service",
            "authority_policy_sha256": policy(
                "authority",
                "capability:executor-authority",
                "The executor is the separately commissioned cuprate diagnostic RPC "
                "service of the ARL-2 dry-run window; it holds no kernel signing "
                "authority and no scientific authority.",
            ),
            "credential_class": "unix.socket.peer-cred",
            "required_independence_groups": ["arl2-service-uid"],
        },
        "runtime": {
            "runtime_kind": contract["runtime_kind"],
            "adapter_ref": contract["adapter_ref"],
            "implementation_sha256": implementation_sha,
            "environment_sha256": environment_sha,
            "determinism": "frozen_seeds",
            "frozen_seeds": [0],
            "maximum_wall_time_seconds": _MAX_WALL_TIME_SECONDS,
            "checkpoint_supported": False,
            "reconciliation_supported": False,
        },
        "applicability": {
            "epistemic_kinds": ["hypothesis_discrimination"],
            "domain_tags": ["materials", "superconductivity"],
            "required_condition_sha256s": sorted(
                [
                    policy(
                        "required_service_contract",
                        "capability:applicability:service-contract",
                        "The operation is bound to the retained local service contract "
                        "for run_cuprate_diagnostic and its retained source closure.",
                    ),
                    contract_sha,
                ]
            ),
            "excluded_condition_sha256s": [
                policy(
                    "excluded_external_replication",
                    "capability:applicability:excluded",
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
                    "capability:safety:hazard",
                    "The dataset is external (UCI superconductivity); the card "
                    "carries license_status to_verify and the campaign verifies "
                    "license and citation at data registration before any egress.",
                )
            ],
            "approval_policy_sha256": policy(
                "safety_approval",
                "capability:safety:approval",
                "Read-only deterministic analysis inside the ARL-2 dry-run window; "
                "approval rides the window's commissioning authority.",
            ),
        },
        "license_egress": {
            "license_policy_sha256": policy(
                "license",
                "capability:license",
                "The UCI superconductivity dataset's license and citation "
                "obligations are verified at data registration (card "
                "license_status to_verify); outputs stay inside campaign custody.",
            ),
            "permitted_input_classes": ["public"],
            "output_license_ids": ["internal-dryrun"],
            "network_egress": contract["network_egress"],
            "egress_policy_sha256": policy(
                "egress",
                "capability:egress",
                "No network egress; the service reads the staged dataset path and "
                "returns its typed result over the pinned unix socket.",
            ),
            "retention_policy_sha256": policy(
                "retention",
                "capability:retention",
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
    if rule_sha != engineering.canonical_sha256(engineering.RULE):
        _fail("the retained qualification rule bytes hash away from the rule pin")

    draft = CapabilityManifestV2.model_validate(base)
    definition = engineering.definition_sha256(draft)
    required_sources = engineering.contract_sources(source_root, draft)
    required_sources.update(service_materials)
    required_sources.update({implementation_sha, environment_sha})
    for digest in sorted(required_sources):
        engineering.source(source_root, digest)

    # 4. check results, signed audit receipts, and the qualification decision.
    def issue(name: str, kind: str, issued_at: datetime, materials, **fields) -> str:
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
            "signature_ed25519_hex": keys[name]["signer"]
            .sign(_canonical_bytes(message))
            .hex(),
        }
        return put(_canonical_bytes(record))

    audit_kinds = tuple(
        sorted(
            (kind for kind in CapabilityAuditKind if kind is not CapabilityAuditKind.CALIBRATION),
            key=lambda kind: kind.value,
        )
    )
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
        now,
        list(audits.values()),
        result="qualified",
        audit_receipt_sha256s=sorted(audits.values()),
    )

    base["qualification"] = {
        "status": "qualified",
        "qualification_rule_sha256": rule_sha,
        "evidence_receipt_sha256s": sorted([*audits.values(), decision]),
        "qualified_by_principal_id": pins["qualifier"]["principal_id"],
        "qualified_at": now.isoformat(),
        "expires_at": receipt_expires_at.isoformat(),
    }
    manifest = CapabilityManifestV2.model_validate(base)
    if engineering.definition_sha256(manifest) != definition:
        _fail("the frozen manifest's definition moved away from the audited draft")
    for kind in audit_kinds:
        if expected_capability_audit_policy_sha256(manifest, kind) != policy_pins[kind.value]:
            _fail(
                f"audit policy for {kind.value} moved between draft and frozen "
                "manifest; the retained receipts no longer bind"
            )

    bindings = [
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

    # 5. catalogs, trust, runtime inventory.
    catalog = CapabilityCatalog(manifests=(manifest,))
    facts = json.loads(host_facts_path.read_text())
    if not isinstance(facts, dict):
        _fail("host facts JSON is not an object")
    resource_catalog = StaticResourceCatalog.model_validate(
        {
            "schema_name": "aletheia.static_resource_catalog",
            "schema_version": 1,
            "catalog_key": f"{_WINDOW_LABEL}.cuprate-diagnostic-external.v1",
            "resource_classes": [
                _resource_class_from_facts(
                    facts,
                    args.cpu_cores,
                    args.memory_bytes,
                    args.scratch_bytes,
                    action_kind=_OPERATION,
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
    # copy is a config output, not just a CAS object
    contract_copy_path, contract_copy_sha = _write_canonical(
        working_root / "configs" / "arl2-cuprate-service-contract.json", contract
    )
    if contract_copy_sha != contract_sha:
        _fail("the retained service contract copy hashes away from the contract pin")
    runtime_sources = {
        manifest.manifest_sha256: {
            "adapter_ref": manifest.runtime.adapter_ref,
            "implementation_path": str(service.implementation_path),
            "implementation_sha256": implementation_sha,
            "environment_sha256": environment_sha,
            "environment_source_sha256": environment_sha,
            "environment_source_path": str(environment_path),
            "service_operation": _OPERATION,
            "service_contract_sha256": contract_sha,
            "service_contract_path": contract_copy_path,
            "service_source_paths": {
                name: str(path) for name, path in service.source_paths.items()
            },
        }
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

    stub_requirement = CapabilityRequirement(
        requirement_id="req.arl2dry.cuprate_diagnostic",
        operation_id=f"operation.{_OPERATION}",
        capability_id=_CAPABILITY_ID,
        semantic_version=_SEMANTIC_VERSION,
        manifest_sha256=manifest.manifest_sha256,
        audit_bindings=[CapabilityAuditBinding.model_validate(item) for item in bindings],
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
        steps=(
            NS(
                role=NS(value=contract["behavior"]["role"]),
                expected_artifacts=(),
                archived_observation_input=None,
                capability_requirement=stub_requirement,
            ),
        ),
    )
    engineering.verify_capability_sources(
        NS(capability_catalog=NS(manifests=(manifest,)), protocol=stub_protocol),
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
            "capability_id": _CAPABILITY_ID,
            "semantic_version": _SEMANTIC_VERSION,
            "operation": _OPERATION,
            "definition_sha256": definition,
            "manifest_sha256": manifest.manifest_sha256,
            "catalog_path": catalog_path,
            "catalog_sha256": catalog_sha,
            "resource_catalog_path": resource_path,
            "resource_catalog_sha256": resource_sha,
            "trust_path": trust_path,
            "trust_sha256": trust_sha,
            "runtime_sources_path": runtime_path,
            "runtime_sources_sha256": runtime_sha,
            "source_root": str(source_root),
            "service_contract_path": contract_copy_path,
            "service_contract_sha256": contract_copy_sha,
            "resource_class_values": {
                "cpu_cores": args.cpu_cores,
                "memory_bytes": args.memory_bytes,
                "scratch_bytes": args.scratch_bytes,
                "provenance": "operator-sampled on the box (nproc, /proc/meminfo, statvfs)",
            },
            "auditor": pins["auditor"],
            "qualifier": pins["qualifier"],
            "audit_bindings": bindings,
            "evidence_receipt_sha256s": sorted([*audits.values(), decision]),
            "valid_from": pin_valid_from.isoformat(),
            "expires_at": pin_expires_at.isoformat(),
            "receipt_expires_at": receipt_expires_at.isoformat(),
            "implementation_sha256": implementation_sha,
            "environment_manifest_path": str(environment_path),
            "environment_sha256": environment_sha,
            "generated_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
        },
    )

    sys.stdout.write(
        f"capability {_CAPABILITY_ID} {_SEMANTIC_VERSION} qualified and cataloged\n"
        f"  manifest   {manifest.manifest_sha256}\n"
        f"  catalog    {catalog_path} ({catalog_sha})\n"
        f"  resources  {resource_path} ({resource_sha})\n"
        f"  audits     {len(audits)} receipts, decision {decision}\n"
        f"  state      {state_path} ({state_sha})\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
