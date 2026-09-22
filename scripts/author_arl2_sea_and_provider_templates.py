#!/usr/bin/env python3
"""Author the per-round ARL-2 protocol-compilation template and the frozen
scientific-execution-authorization template (commissioning kit, design 8).

Two modes, one per driver pause:

--mode provider (PAUSE-1). Runs AFTER the round's ACTION_AUTHORIZED event has
committed to the kernel stream and BEFORE the compile tick consumes it (the
driver is stopped). The script re-bases the staged, PI-reviewed protocol body
onto the audited graph scope, pre-binds the protocol budget to a freshly
authored source budget, injects the round-split caller parameters pinned by
the compilation policy, runs the offline compiler, and writes one
FrozenProtocolCompilationTemplate plus the source-budget sidecar that PAUSE-2
re-reads.

--mode sea (PAUSE-2). Runs AFTER the compile tick registered the compilation
row and BEFORE the execution-authorization tick. The script reads the
registered compilation from PostgreSQL, audits the kernel stream for the
ACTION_PROPOSED/ACTION_AUTHORIZED events, authors the zero-cost quote, the
source budget and its exact projection, appends them to the frozen authority
registry (thaw-append-restore, then registry re-instantiation as the
self-check), issues the engineering-qualification grant, and assembles the
FrozenScientificExecutionAuthorizationTemplate. A catalog dry-run over the
deployment pins re-derives the full SEA message offline; the deployed service
still produces the live signature at campaign time. The kernel audit and the
compilation-row read run in a forked child that drops to the driver identity
(the CAS owner): the kernel archive and the registry append never share one
effective uid, and the child mirrors the writer-handle audit the runtime
itself performs (the owner-only CAS custody admits no other reader).

Protocol inputs (contradiction #18 remedy): every executor input port with no
WorkOrder producer must be declared with --protocol-input PORT=PATH. The bytes
are admitted through the deployment artifact store's real custody chain
(quarantine -> central rehash -> CAS -> manifest -> verified receipt) under a
dedicated admission replicate slot and infrastructure attempt — an identity
that never gains terminal-archive rows, so later fresh resolutions of the
bound receipt cannot collide with an unrelated producer lineage. The intent
binds one protocol_input binding per port; work-order lineage inputs stay
refused in first-round commissioning.

Provenance rules:

- action identity comes from the spool submission (action.object_sha256 is a
  computed property; nothing stores it).
- --graph-snapshot-sha256 and --action-authorized-committed-at are operator
  inputs copied from the kernel audit at ACTION_AUTHORIZED; the compile tick
  will re-derive them and byte-compare, so a typo fails closed there at the
  latest.
- the four campaign digests (--observation-namespace-sha256,
  --selection-campaign-sha256, --prediction-campaign-sha256,
  --prediction-commitment-sha256) are copied verbatim from the F9
  preregistration campaign records; no port resolves them today.
- validator_manifest_sha256 is the independent_validation service manifest
  sha from the deployment state; observation_validation_policy_sha256 is the
  deployed validator bridge policy sha. No state surface pins either from the
  qualification side; the choice is documented here and the closure that
  matters (admission policy vs message) is checked by the dry-run.
- staged predictions commit predicted_outcome_sha256 over the staged
  observable/measurement/outcome-space triple. A re-base cannot re-derive the
  commitment (no outcome bin id survives on the frozen prediction model), so
  any prediction whose hash changes under re-basing fails loudly: stage the
  body against the audited graph scope.

Timing defaults: intent.authorized_at equals the source authorized_at;
intent.deadline is the protocol deadline minus 3600 s (the SEA chain needs
intent.deadline strictly below the observation admission deadline); the quote
is stamped now; the grant window is bounded by the five minima the issuer
checks. All derived instants are asserted, not trusted.

Known failure mode: registry appends burn uniqueness keys (one quote digest
per infrastructure attempt, one projection per source/budget/resource
budget). Every operator-fixable rejection — grant/SEA window derivation,
outcome bins, admission policy, artifact binding, campaign digest patterns,
release root, output path — therefore runs before the first append; only
grant issuance, template assembly, and the registry self-checks follow the
appends. If the script still dies between append and template write, re-run
with a fresh --source-budget-id and an adjusted protocol deadline; never
write a second projection for the same resource budget. A crash mid-append
can also leave a half-written <digest>.json/.sig pair in non-pinned custody;
remove it as root before the fresh-id re-run can proceed.

The ACTION_PROPOSED/ACTION_AUTHORIZED committer is an open design question
(nothing in the examined topology commits them); if the events are absent the
audit fails loudly and the campaign stalls before this script can help.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_SCHEMA = "aletheia.arl2_deployment_state"

ROUND_SPLIT_PARAMETER_IDS = (
    "dataset_content_sha256",
    "round_bound_batch_group_ids",
    "round_sealed_group_ids",
    "round_spent_group_ids",
    "round_unspent_group_ids",
)

INTENT_DEADLINE_MARGIN_SECONDS = 3600


def _fail(message: str) -> None:
    print(f"author_arl2_sea_and_provider_templates: {message}", file=sys.stderr)
    raise SystemExit(1)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


_ARTIFACT_KIND_MEDIA_TYPES = {
    "json": "application/json",
    "table": "text/csv",
    "text": "text/plain",
    "binary": "application/octet-stream",
}


def _protocol_input_media_type(artifact_kind) -> str:
    value = getattr(artifact_kind, "value", artifact_kind)
    media_type = _ARTIFACT_KIND_MEDIA_TYPES.get(value)
    if media_type is None:
        _fail(
            f"no canonical media type for artifact kind {value!r}; the port contract "
            "must settle on one of " + ", ".join(sorted(_ARTIFACT_KIND_MEDIA_TYPES))
        )
    return media_type


def _protocol_input_requirement(*, port, retention_policy_sha256: str, max_bytes: int):
    """ExpectedArtifact for one protocol-level input port (contradiction #18).

    role stays RAW_OUTPUT because the qualification input check resolves every
    bound receipt to a verified raw-output artifact; schema/classification come
    from the frozen protocol port, retention from the selected capability's
    license/egress contract — the same source the compiled output artifacts use.
    """

    from aletheia.execution.schemas import ArtifactRole, ExpectedArtifact

    return ExpectedArtifact(
        artifact_key=port.port_id,
        role=ArtifactRole.RAW_OUTPUT,
        media_type=_protocol_input_media_type(port.artifact_kind),
        schema_sha256=port.schema_ref.schema_sha256,
        data_classification=getattr(port.data_classification, "value", port.data_classification),
        retention_policy_sha256=retention_policy_sha256,
        max_bytes=max_bytes,
    )


def _produced_output_port_ids(work_order) -> set:
    """Output ports ANY work-order node produces (contradiction #18).

    Executor inputs split on this set: the intersection is work-order lineage
    (first-round commissioning refuses it; continuation rounds carry it), the
    difference is protocol-level inputs bound through admitted receipts.
    """

    return {
        port_id for other in work_order.nodes for port_id in other.output_port_ids
    }


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


def _write_new_file(path: Path, payload: bytes, *, mode: int) -> str:
    if path.exists():
        _fail(f"{path} already exists (write-once discipline)")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
    os.chmod(path, mode)
    return _sha256_bytes(payload)


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


def _sign_raw(private_path: Path, message: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    material = _read_bytes(private_path)
    if len(material) != 32:
        _fail(f"{private_path} is not a raw 32-byte Ed25519 private key")
    return Ed25519PrivateKey.from_private_bytes(material).sign(message)


def _load_state(path: Path) -> dict:
    state = _load_json(path)
    if state.get("schema_name") != STATE_SCHEMA:
        _fail(f"{path} is not an ARL-2 deployment state file")
    return state


def _assert_window(label: str, valid_from: datetime, active_until: datetime, now: datetime) -> None:
    if not valid_from <= now < active_until:
        _fail(f"{label} window [{_iso(valid_from)}, {_iso(active_until)}) does not contain now")


# ---------------------------------------------------------------------------
# provider mode: re-base, pre-bind, inject, compile, freeze
# ---------------------------------------------------------------------------


def _remap_values(obj, mapping: dict[str, str], key: str | None = None):
    """Replace hash values that are keys of mapping by their new hashes.

    Substitution only happens under dict keys named like hash fields
    (*_sha256 / *_sha256s, propagated through lists and tuples), so prose
    that happens to quote an old digest is never rewritten.  Tuples must
    walk like lists because model_dump(mode="python") preserves tuple
    fields as tuples: every cross-reference container in the protocol
    schemas (endpoint sets, observable/assumption sets, epistemic targets,
    world-model derived_from/discriminates_from, belief entries, and the
    step tuple itself) would otherwise pass through untouched.

    A multi-hash container whose key ends in _sha256s and whose entries the
    mapping actually moves is a canonical set: those validators require the
    entries sorted and unique, and remapping is not order-preserving, so
    moved entries are re-collapsed and re-sorted here. Containers the
    mapping does not touch (positional commitments like replicate seeds)
    keep their staged order untouched.
    """

    if isinstance(obj, dict):
        return {name: _remap_values(value, mapping, name) for name, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        if (
            key is not None
            and key.endswith("_sha256s")
            and obj
            and all(isinstance(item, str) for item in obj)
        ):
            moved = [mapping.get(item, item) for item in obj]
            if moved != list(obj):
                return tuple(sorted(set(moved)))
            return tuple(obj) if isinstance(obj, tuple) else list(obj)
        if isinstance(obj, tuple):
            return tuple(_remap_values(item, mapping, key) for item in obj)
        return [_remap_values(item, mapping, key) for item in obj]
    if (
        isinstance(obj, str)
        and key is not None
        and (key.endswith("_sha256") or key.endswith("_sha256s"))
        and obj in mapping
    ):
        return mapping[obj]
    return obj


def _rehash_collection(items: tuple, model_class, sha_property: str, combined: dict[str, str]) -> tuple:
    """Re-stamp one world-model collection until its cross-references close.

    Items reference each other by content hash (derived_from_*, hypothesis
    refs, discriminates_from), so a single forward pass is not enough when a
    referenced item sits later in the tuple. Each pass re-maps the current
    items through the accumulated map; the map stabilizes at the reference-DAG
    depth, which is bounded by the collection length. Identity self-maps never
    count as change, and on exit every ORIGINAL hash is collapsed to the final
    hash so single-pass consumers never bind an intermediate pass hash.
    """

    items = list(items)
    original_hashes = [getattr(item, sha_property) for item in items]
    for _ in range(len(items) + 2):
        pass_map: dict[str, str] = {}
        rebuilt = []
        for item in items:
            data = _remap_values(item.model_dump(mode="python"), combined)
            candidate = model_class.model_validate(data)
            pass_map[getattr(item, sha_property)] = getattr(candidate, sha_property)
            rebuilt.append(candidate)
        changed = False
        for old, new in pass_map.items():
            if old != new and combined.get(old) != new:
                combined[old] = new
                changed = True
        items = rebuilt
        if not changed:
            for original, item in zip(original_hashes, items):
                combined[original] = getattr(item, sha_property)
            return tuple(items)
    _fail("world-model reference graph did not stabilize while re-basing")


def _rebase_protocol(staged, graph_scope) -> tuple:
    """Re-stamp the staged protocol onto the audited graph scope.

    Returns (protocol_data, contract_hashes) where protocol_data is the
    re-based model_dump and contract_hashes maps every old step-contract
    hash to its re-hashed replacement. Follows the re-basing mechanics of
    tests/research_controller/test_vertical_cut.py extended to a staged
    (not freshly built) world model.
    """

    from aletheia.protocols.schemas import AnalysisPlan, ProtocolIR

    old_scope_sha = staged.graph_scope.graph_scope_sha256
    new_scope_sha = graph_scope.graph_scope_sha256
    combined: dict[str, str] = {old_scope_sha: new_scope_sha}

    data = staged.model_dump(mode="python")

    # graph-scope-carried sections: the map covers their graph_scope_sha256
    objective_model = type(staged.objective).model_validate(
        _remap_values(data["objective"], combined)
    )
    design_space_model = type(staged.design_space).model_validate(
        _remap_values(data["design_space"], combined)
    )
    method_model = type(staged.method).model_validate(_remap_values(data["method"], combined))
    claim_model = type(staged.claim_contract).model_validate(
        _remap_values(data["claim_contract"], combined)
    )

    # observables re-hash, and everything referencing them must follow
    observables = []
    for item in staged.observables:
        model = type(item).model_validate(_remap_values(item.model_dump(mode="python"), combined))
        combined[item.observable_sha256] = model.observable_sha256
        observables.append(model)
    observable_bindings = []
    for item in staged.observable_output_bindings:
        model = type(item).model_validate(
            _remap_values(item.model_dump(mode="python"), combined)
        )
        observable_bindings.append(model)
    observable_bindings.sort(key=lambda item: item.observable_spec_sha256)

    # analysis plan endpoints are observable hashes; its outcome space re-hashes
    old_plan = staged.analysis_plan
    plan_model = AnalysisPlan.model_validate(_remap_values(data["analysis_plan"], combined))
    from aletheia.research_kernel.schemas import canonical_sha256

    combined[old_plan.outcome_space_sha256] = plan_model.outcome_space_sha256

    # world model: re-stamp scope, then re-hash hypotheses/assumptions/
    # predictions/belief to a fixpoint, then rebuild the snapshot
    world_model_model = None
    if staged.world_model is not None:
        from aletheia.protocols.world_models import (
            AssumptionVersionV2,
            BeliefStateVersionV2,
            HypothesisVersionV2,
            PredictionVersionV2,
        )

        hypotheses = _rehash_collection(
            staged.world_model.hypotheses, HypothesisVersionV2, "hypothesis_sha256", combined
        )
        assumptions = _rehash_collection(
            staged.world_model.assumptions, AssumptionVersionV2, "assumption_sha256", combined
        )
        predictions = _rehash_collection(
            staged.world_model.predictions, PredictionVersionV2, "prediction_sha256", combined
        )
        for staged_prediction, prediction in zip(staged.world_model.predictions, predictions):
            if staged_prediction.prediction_sha256 != prediction.prediction_sha256:
                _fail(
                    f"prediction {staged_prediction.prediction_id} re-hashes under the "
                    "audited scope; its predicted_outcome_sha256 commits over the staged "
                    "observable/measurement/outcome-space triple and cannot be re-derived "
                    "(no outcome bin id survives on the frozen model). Stage the protocol "
                    "body against the audited graph scope."
                )
        wm_data = staged.world_model.model_dump(mode="python")
        wm_data["graph_scope"] = graph_scope.model_dump(mode="python")
        wm_data["hypotheses"] = [item.model_dump(mode="python") for item in hypotheses]
        wm_data["assumptions"] = [item.model_dump(mode="python") for item in assumptions]
        wm_data["predictions"] = [item.model_dump(mode="python") for item in predictions]
        if staged.world_model.belief_state is not None:
            belief_data = _remap_values(wm_data["belief_state"], combined)
            # the validator requires belief entries sorted by hypothesis hash;
            # remapping moves those hashes, so the entries re-sort here
            if belief_data.get("hypothesis_beliefs"):
                belief_data["hypothesis_beliefs"] = tuple(
                    sorted(belief_data["hypothesis_beliefs"], key=lambda e: e["hypothesis_sha256"])
                )
            belief = BeliefStateVersionV2.model_validate(belief_data)
            wm_data["belief_state"] = belief.model_dump(mode="python")
        from aletheia.protocols.world_models import WorldModelSnapshotV2

        world_model_model = WorldModelSnapshotV2.model_validate(wm_data)
        combined[staged.world_model.world_model_sha256] = world_model_model.world_model_sha256

    # epistemic contract: scope, world-model snapshot, hypothesis targets,
    # and any observable references all re-map through the combined map
    epistemic_model = type(staged.epistemic_contract).model_validate(
        _remap_values(data["epistemic_contract"], combined)
    )

    # controls reference observable spec hashes
    controls = []
    for item in staged.controls:
        model = type(item).model_validate(_remap_values(item.model_dump(mode="python"), combined))
        controls.append(model)

    contract_hashes: dict[str, str] = {
        staged.objective.objective_sha256: objective_model.objective_sha256,
        staged.design_space.design_space_sha256: design_space_model.design_space_sha256,
        staged.method.method_sha256: method_model.method_sha256,
        staged.epistemic_contract.contract_sha256: epistemic_model.contract_sha256,
        canonical_sha256(data["analysis_plan"]): canonical_sha256(
            plan_model.model_dump(mode="python")
        ),
    }
    for old_control, new_control in zip(staged.controls, controls):
        contract_hashes[canonical_sha256(old_control.model_dump(mode="python"))] = canonical_sha256(
            new_control.model_dump(mode="python")
        )

    merged = dict(data)
    merged.update(
        graph_scope=graph_scope.model_dump(mode="python"),
        objective=objective_model.model_dump(mode="python"),
        design_space=design_space_model.model_dump(mode="python"),
        method=method_model.model_dump(mode="python"),
        epistemic_contract=epistemic_model.model_dump(mode="python"),
        claim_contract=claim_model.model_dump(mode="python"),
        observables=[item.model_dump(mode="python") for item in observables],
        observable_output_bindings=[item.model_dump(mode="python") for item in observable_bindings],
        analysis_plan=plan_model.model_dump(mode="python"),
        controls=[item.model_dump(mode="python") for item in controls],
        # steps carry observable references too (archived observation inputs,
        # observable output bindings); remap them through the same map. Step
        # contract hashes are not in this map's domain, so bindings pass
        # through untouched for _repin_step_contracts.
        steps=_remap_values(data["steps"], combined),
    )
    if world_model_model is not None:
        merged["world_model"] = world_model_model.model_dump(mode="python")
    return ProtocolIR.model_validate(merged), contract_hashes


def _inject_round_split(protocol, values: dict[str, str], value_schema, contract_hashes: dict[str, str]):
    """Wire the five card-derived caller parameters the compiler demands.

    The parameter-id literals live only in protocol_compilation_step.py; this
    injection mirrors tests/research_controller/test_protocol_compilation_step.py
    956-1027: covariate caller-mutable factors, bindings on the protocol,
    ids on the scientific-executor step only, manifest re-hash, design-space
    contract bindings re-pinned. assignment_rule_sha256 is a deterministic
    synthetic digest: these covariates are pinned by policy, no rule document
    exists to hash.
    """

    from aletheia.protocols.schemas import (
        ProtocolIR,
        ProtocolStepRole,
        caller_parameter_manifest_sha256 as manifest_sha256,
    )

    staged = protocol
    data = staged.model_dump(mode="python")

    merged_bindings = {item["parameter_id"]: item for item in data["caller_parameter_bindings"]}
    for parameter_id, value_sha in values.items():
        merged_bindings[parameter_id] = {
            "parameter_id": parameter_id,
            "value_sha256": value_sha,
        }
    bindings = [merged_bindings[key] for key in sorted(merged_bindings)]

    factors = {item["factor_id"]: item for item in data["design_space"]["factors"]}
    for parameter_id in values:
        if parameter_id in factors:
            _fail(
                f"staged design space already carries a factor with the compiler's "
                f"caller-parameter id {parameter_id}; the injection must not "
                "overwrite a staged factor"
            )
    for parameter_id in values:
        factors[parameter_id] = {
            "factor_id": parameter_id,
            "factor_kind": "covariate",
            "value_schema": value_schema.model_dump(mode="python"),
            "assignment_rule_sha256": _sha256_bytes(
                f"arl2-kit:caller-parameter-assignment:{parameter_id}".encode("utf-8")
            ),
            "caller_mutable": True,
        }
    data["design_space"]["factors"] = [factors[key] for key in sorted(factors)]

    steps = []
    for step in data["steps"]:
        if step["role"] == ProtocolStepRole.SCIENTIFIC_EXECUTOR.value:
            ids = sorted(set(step["caller_parameter_ids"]) | set(values))
            step["caller_parameter_ids"] = ids
        elif step["caller_parameter_ids"]:
            _fail(
                f"step {step['step_id']} carries caller parameters outside the "
                "scientific-executor role"
            )
        steps.append(step)
    data["steps"] = steps
    data["caller_parameter_bindings"] = bindings
    data["caller_parameter_manifest_sha256"] = manifest_sha256(
        tuple(
            type(staged.caller_parameter_bindings[0]).model_validate(item)
            if staged.caller_parameter_bindings
            else _binding_model(item)
            for item in bindings
        )
    )

    protocol = ProtocolIR.model_validate(data)
    contract_hashes = dict(contract_hashes)
    contract_hashes[staged.design_space.design_space_sha256] = protocol.design_space.design_space_sha256
    return protocol, contract_hashes


def _binding_model(item: dict):
    from aletheia.protocols.schemas import CallerParameterBinding

    return CallerParameterBinding.model_validate(item)


def _resolve_contract_hash(hash_value: str, contract_hashes: dict[str, str]) -> str:
    """Follow contract_hashes transitively: the design space re-hashes once
    in the re-base (scope change) and again at round-split injection, so a
    staged binding carries the ORIGINAL hash while the map holds a two-edge
    chain. Single .get() lookups resolve to the intermediate hash and the
    compiler's contract-set gate rejects the protocol."""

    seen: set[str] = set()
    while hash_value in contract_hashes and hash_value not in seen:
        seen.add(hash_value)
        successor = contract_hashes[hash_value]
        if successor == hash_value:
            break
        hash_value = successor
    return hash_value


def _repin_step_contracts(protocol, contract_hashes: dict[str, str]):
    """Re-point every step contract binding at its re-hashed contract."""

    from aletheia.protocols.schemas import ProtocolIR

    data = protocol.model_dump(mode="python")
    for step in data["steps"]:
        bindings = [
            {
                "contract_kind": item["contract_kind"],
                "contract_sha256": _resolve_contract_hash(
                    item["contract_sha256"], contract_hashes
                ),
            }
            for item in step["contract_bindings"]
        ]
        bindings.sort(
            key=lambda item: (
                f"{getattr(item['contract_kind'], 'value', item['contract_kind'])}"
                f":{item['contract_sha256']}"
            )
        )
        step["contract_bindings"] = bindings
    return ProtocolIR.model_validate(data)


def _assert_executor_rigidity(result) -> None:
    """SEA phase-1 rigidity must already hold in the compiled work order."""

    from aletheia.execution.schemas import ExecutionRetryMode
    from aletheia.protocols.schemas import ProtocolStepRole

    nodes = [node for node in result.work_order.nodes if node.role is ProtocolStepRole.SCIENTIFIC_EXECUTOR]
    if not nodes:
        _fail("compiled work order has no scientific-executor node")
    for node in nodes:
        if (
            node.retry_policy.mode is not ExecutionRetryMode.NEVER
            or node.retry_policy.maximum_attempts_per_scientific_slot != 1
            or node.resource_request.max_infrastructure_attempts != 1
        ):
            _fail(
                f"node {node.node_id} is retryable; first-round SEA requires "
                "mode NEVER, one attempt per slot, one infrastructure attempt"
            )


def _run_provider(args, state: dict, state_path: Path) -> int:
    from aletheia.execution.authority_contracts import SourceBudgetAuthorization
    from aletheia.execution.qualification_custody import QualificationPreAdmissionCustodyConfig
    from aletheia.protocols.compiler import (
        ProtocolCompilationRequest,
        compile_protocol,
        verify_compilation,
    )
    from aletheia.protocols.schemas import ProtocolIR, ProtocolScope
    from aletheia.research_controller.action_proposals import SubmittedActionProposal
    from aletheia.research_controller.protocol_compilation_step import (
        ProtocolCompilationPolicyPin,
    )
    from aletheia.research_controller.protocol_template_provider import (
        FrozenProtocolCompilationTemplate,
    )
    from aletheia.research_kernel.schemas import ActionKind, canonical_json_bytes, canonical_sha256

    policy = ProtocolCompilationPolicyPin.model_validate(state["policy_pins"]["compilation"])

    # catalogs: byte-pinned files, loaded to models; a non-canonical pinned
    # file makes a valid template impossible, so surface divergence loudly
    from aletheia.protocols.capabilities import CapabilityCatalog
    from aletheia.execution.schemas import StaticResourceCatalog

    capability_bytes = _read_bytes(Path(args.capability_catalog))
    resource_bytes = _read_bytes(Path(args.resource_catalog))
    if _sha256_bytes(capability_bytes) != policy.capability_catalog_sha256:
        _fail(f"{args.capability_catalog} bytes differ from the pinned capability catalog")
    if _sha256_bytes(resource_bytes) != policy.resource_catalog_sha256:
        _fail(f"{args.resource_catalog} bytes differ from the pinned resource catalog")
    capability_catalog = CapabilityCatalog.model_validate(json.loads(capability_bytes))
    resource_catalog = StaticResourceCatalog.model_validate(json.loads(resource_bytes))
    if capability_catalog.catalog_sha256 != policy.capability_catalog_sha256:
        _fail("capability catalog model hash differs from the byte pin (file is not canonical)")
    if resource_catalog.catalog_sha256 != policy.resource_catalog_sha256:
        _fail("resource catalog model hash differs from the byte pin (file is not canonical)")

    submission = SubmittedActionProposal.model_validate(_load_json(Path(args.submission)))
    action = submission.action
    action_sha = action.object_sha256
    if action.kind is not ActionKind.DISCRIMINATE:
        _fail(f"action {action_sha} is {action.kind.value}; ARL-2 pins DISCRIMINATE")

    authorized_committed_at = _parse_iso(
        args.action_authorized_committed_at, "--action-authorized-committed-at"
    )
    graph_snapshot = args.graph_snapshot_sha256
    if len(graph_snapshot) != 64 or any(char not in "0123456789abcdef" for char in graph_snapshot):
        _fail("--graph-snapshot-sha256 is not a sha256 hex digest")
    if action.proposed_at > authorized_committed_at:
        _fail("action proposed_at follows the authorization commit; inputs are inconsistent")

    scope_binding = submission.command_proposal.scope_binding
    graph_scope = ProtocolScope(
        scope_binding=scope_binding,
        scope_node_id=(
            scope_binding.campaign_id or scope_binding.program_id or scope_binding.quest_id
        ),
        branch_id=submission.target_branch_id,
        question_ref=action.question_ref,
        graph_snapshot_sha256=graph_snapshot,
    )

    staged = ProtocolIR.model_validate(_load_json(Path(args.protocol_body)))
    budget_contract = staged.resource_budget
    state_expires_at = _parse_iso(state["expires_at"], "state expires_at")
    if budget_contract.deadline > state_expires_at:
        _fail("staged protocol deadline outlives the deployment state window")

    # source budget first: its canonical hash is pinned into the protocol
    custody = QualificationPreAdmissionCustodyConfig.model_validate(
        state["qualification"]["custody"]
    )
    source_pin = custody.source_budget_authority_pin
    source_authorized_at = (
        _parse_iso(args.source_authorized_at, "--source-authorized-at")
        if args.source_authorized_at
        else authorized_committed_at + timedelta(seconds=1)
    )
    if not args.source_expires_at:
        _fail("--source-expires-at is required (the budget window is an operator decision)")
    source_expires_at = _parse_iso(args.source_expires_at, "--source-expires-at")
    source = SourceBudgetAuthorization(
        source_budget_id=args.source_budget_id
        or f"source-budget.{action.quest_id}.r{args.round_index}",
        quest_id=action.quest_id,
        currency_code=budget_contract.currency_code,
        maximum_cost_microunits=budget_contract.maximum_cost_microunits,
        deadline=budget_contract.deadline,
        authorized_by_principal_id=source_pin.principal_id,
        authorized_at=source_authorized_at,
        expires_at=source_expires_at,
        source_authorization_policy_sha256=source_pin.policy_sha256,
        source_authority_key_id=source_pin.key_id,
    )
    if not source_pin.active_at(source_authorized_at) or source.active_until > source_pin.active_until:
        _fail("source budget window escapes the source-budget authority pin")
    source_sha = source.source_budget_authorization_sha256

    rebased, contract_hashes = _rebase_protocol(staged, graph_scope)
    if policy.round_split_binding is not None:
        rows = [
            row
            for row in policy.round_split_binding.template_bindings
            if row.action_sha256 == action_sha
        ]
        if len(rows) != 1:
            _fail("round-split policy has no unique row for the authorized action")
        values = {
            "dataset_content_sha256": policy.round_split_binding.dataset_content_sha256,
            "round_bound_batch_group_ids": rows[0].bound_batch_group_ids_sha256,
            "round_sealed_group_ids": policy.round_split_binding.sealed_group_ids_sha256,
            "round_spent_group_ids": rows[0].spent_group_ids_sha256,
            "round_unspent_group_ids": rows[0].unspent_group_ids_sha256,
        }
    else:
        # Merged channel (PI decision Q13(b), 2026-09-18): the commissioned
        # policy pin stays binding-less and the compile service enforces the
        # campaign request's commissioning-time bindings, loaded from the
        # byte-pinned request file; the five values come from the round's
        # binding (_merged_round_split_values).
        request_entry = state.get("request") or {}
        request_path = request_entry.get("request_path")
        request_file_sha = request_entry.get("request_file_sha256")
        if not request_path or not request_file_sha:
            _fail(
                "deployment state carries no campaign request pin; the merged "
                "round-split channel needs request_path and request_file_sha256 "
                "(re-run author-arl2-deployments.py)"
            )
        request_bytes = _read_bytes(Path(request_path))
        if _sha256_bytes(request_bytes) != request_file_sha:
            _fail("campaign request file differs from its state pin")
        try:
            document = json.loads(request_bytes)
        except ValueError as exc:
            _fail(f"campaign request is not readable JSON: {exc}")
        # same quest tie the deployed compile service enforces on its config
        # pin: the byte-pinned document must belong to THIS deployment's
        # quest, not merely to any quest with matching file bytes
        if document.get("quest_id") != state.get("quest_id"):
            _fail(
                "campaign request belongs to another quest "
                f"({document.get('quest_id')} vs {state.get('quest_id')})"
            )
        values = _merged_round_split_values(document, round_index=args.round_index)
    if set(values) != set(ROUND_SPLIT_PARAMETER_IDS):
        _fail(
            "round-split value keys drift from ROUND_SPLIT_PARAMETER_IDS; the "
            "compiler pins the parameter ids in protocol_compilation_step.py"
        )
    value_schema = staged.data_ports[0].schema_ref
    rebased, contract_hashes = _inject_round_split(rebased, values, value_schema, contract_hashes)

    authored_by = args.author_principal_id or policy.allowed_protocol_author_principal_ids[0]
    if authored_by not in policy.allowed_protocol_author_principal_ids:
        _fail(f"protocol author {authored_by} is not allowed by the compilation policy")
    authored_at = authorized_committed_at + timedelta(seconds=1)

    validator = state["bridge"]["validator"]["principal_id"]
    admission = state["bridge"]["admission"]["principal_id"]
    data = rebased.model_dump(mode="python")
    data["authored_by_principal_id"] = authored_by
    data["authored_at"] = authored_at
    data["independence"]["validator_principal_ids"] = [validator]
    data["independence"]["claim_approver_principal_ids"] = [admission]
    data["resource_budget"]["budget_authorization_sha256"] = source_sha
    protocol = _repin_step_contracts(
        type(rebased).model_validate(data), contract_hashes
    )

    # mirror the compile-time verify gates with local failure text
    try:
        allowed = policy.allowed_categories_for(action.kind)
    except Exception as exc:  # ProtocolCompilationUnavailable carries a tuple payload
        _fail(f"compilation policy does not enable action kind {action.kind.value}: {exc}")
    if protocol.objective.action_category not in allowed:
        _fail("staged objective category is not allowed for the action kind")
    if action.kind in policy.world_model_required_action_kinds and protocol.world_model is None:
        _fail("action kind requires a world model; the staged body carries none")

    request = ProtocolCompilationRequest(
        protocol=protocol,
        capability_catalog=capability_catalog,
        resource_catalog=resource_catalog,
        compiler_implementation_sha256=policy.compiler_implementation_sha256,
    )
    result = compile_protocol(request)
    verify_compilation(request, result)
    if not result.report.accepted or result.work_order is None:
        blockers = "; ".join(item.code.value for item in result.report.blockers)
        _fail(f"protocol does not compile cleanly (blockers: {blockers or 'unknown'})")
    _assert_executor_rigidity(result)

    # mirror the deployed compile tick's revision gates: the belief-basis
    # assert is identical offline, while the registered-parent checks need
    # the registry rows and can only run at the tick itself — a revision-
    # shaped staged body gets an explicit note instead of a silent pass
    if protocol.world_model is not None and protocol.world_model.version > 1:
        from aletheia.research_controller.world_model_revision import (
            assert_revision_belief_basis,
        )

        try:
            assert_revision_belief_basis(protocol.world_model)
        except Exception as exc:
            _fail(f"staged world-model revision fails the belief-basis assert: {exc}")
        print(
            f"NOTE staged world model is a revision (version "
            f"{protocol.world_model.version}); registration additionally requires "
            "the exact registered parent snapshot, which only the deployed "
            "compile tick can verify",
            file=sys.stderr,
        )
    if protocol.version > 1:
        print(
            f"NOTE staged protocol is a revision (version {protocol.version}); "
            "registration additionally requires the contiguous registered parent "
            "row and verify_authored_revision_v2 at the deployed compile tick",
            file=sys.stderr,
        )

    template = FrozenProtocolCompilationTemplate(
        action_sha256=action_sha,
        action_kind=action.kind,
        request_sha256=canonical_sha256(request),
        request=request,
    )

    output = Path(args.output)
    from aletheia.execution.schemas import canonical_json_bytes as execution_canonical_bytes

    sidecar = Path(f"{output}.source-budget.json")
    _write_new_file(sidecar, execution_canonical_bytes(source), mode=0o644)
    _write_new_file(output, canonical_json_bytes(template), mode=0o644)

    summary = {
        "mode": "provider",
        "action_sha256": action_sha,
        "action_kind": action.kind.value,
        "quest_id": action.quest_id,
        "protocol_sha256": protocol.protocol_sha256,
        "request_sha256": template.request_sha256,
        "template_sha256": template.template_sha256,
        "graph_scope_sha256": graph_scope.graph_scope_sha256,
        "source_budget_id": source.source_budget_id,
        "source_budget_authorization_sha256": source_sha,
        "source_budget_expires_at": _iso(source_expires_at),
        "output": str(output),
        "source_budget_sidecar": str(sidecar),
    }
    print("PROVIDER_TEMPLATE " + json.dumps(summary, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# sea mode: read the registered compilation, append the registry, issue the
# grant, freeze the SEA template
# ---------------------------------------------------------------------------


class _FailClosedArtifactResolver:
    """First-round qualification without input bindings never calls the input
    resolver; refuse loudly if that assumption breaks."""

    def resolve_artifact_manifest(self, *, manifest_sha256: str, observed_at: datetime):
        _fail("artifact manifests cannot be resolved during first-attempt commissioning")

    def resolve_verified_input_artifact(self, *, verified_receipt_sha256: str, observed_at: datetime):
        _fail("input artifacts cannot be resolved during first-attempt commissioning")


class _EmptyTerminalArchive:
    """Offline dry-run stand-in for the terminal receipt archive.

    Admission attempts never gain terminal rows by construction (their slot
    identity exists only for the admission), so an empty listing is the exact
    offline truth, not a stub that hides lineage."""

    def list_terminal_receipts_for_attempt(self, *, infrastructure_attempt_id: str):
        return ()


class _FailClosedReceiptResolver:
    def resolve_execution_receipt(self, *, execution_receipt_sha256: str, observed_at: datetime):
        _fail("execution receipts cannot be resolved during first-attempt commissioning")


def _load_worker_config(state: dict):
    """Byte-pin and parse the worker composition; no archive opens here (the
    kernel archive and the registry append must never share one effective
    uid, so the archive and everything downstream of it live in the forked
    audit child at the driver identity)."""

    from aletheia.research_controller.worker_composition import (
        ControllerWorkerCompositionError,
        load_research_controller_worker_runtime_config,
    )

    worker_path = Path(state["worker"]["configuration_path"])
    worker_bytes = _read_bytes(worker_path)
    if _sha256_bytes(worker_bytes) != state["worker"]["configuration_file_sha256"]:
        _fail("worker composition bytes differ from the state file pin")
    try:
        return load_research_controller_worker_runtime_config(worker_bytes)
    except (ControllerWorkerCompositionError, TypeError, ValueError) as exc:
        _fail(f"worker composition no longer parses: {exc}")


def _merged_round_split_values(document, *, round_index: int) -> dict:
    """Derive one round's five round-split values from a parsed campaign request.

    Merged channel (PI decision Q13(b), 2026-09-18): the request's template
    rows key on placeholder action identities (unknowable at authoring time),
    so this selects the round's binding by round_index and requires exactly
    one template row; the deployed compile gate then matches the five values
    uniquely across every row.
    """
    from aletheia.research_controller.protocol_compilation_step import (
        RoundSplitBindingPolicyV1,
    )

    try:
        raw_bindings = document["round_split_bindings"]
    except (KeyError, TypeError) as exc:
        _fail(f"campaign request carries no readable round_split_bindings: {exc}")
    bindings = [RoundSplitBindingPolicyV1.model_validate(item) for item in raw_bindings]
    selected = [item for item in bindings if item.round_index == round_index]
    if len(selected) != 1:
        _fail(f"campaign request has no unique round {round_index} binding")
    binding = selected[0]
    if len(binding.template_bindings) != 1:
        _fail(
            "merged round-split authoring requires exactly one template row "
            "per round binding (the deployed gate matches values uniquely)"
        )
    row = binding.template_bindings[0]
    return {
        "dataset_content_sha256": binding.dataset_content_sha256,
        "round_bound_batch_group_ids": row.bound_batch_group_ids_sha256,
        "round_sealed_group_ids": binding.sealed_group_ids_sha256,
        "round_spent_group_ids": row.spent_group_ids_sha256,
        "round_unspent_group_ids": row.unspent_group_ids_sha256,
    }


def _kernel_audit_child(worker, database_url: str, quest_id: str, action_sha: str, out_path: Path) -> None:
    """Run inside the forked child at the driver identity (the CAS owner).

    The deployed topology pins the kernel CAS to the writer's custody mode
    (arl2_runtime composes the writer at the configured root mode, 0700 or
    the 0750 shared-custody widen), so the standalone read-only
    composition is structurally unavailable for this child: cas.py's
    read-only branch gives the owner the owner permission class, which on
    a writer root always carries the write bit. The one audit path that
    exists in this deployment is the driver's own: open the writer archive
    as the root's owner identity and audit through the kernel store,
    exactly what arl2_runtime does for its role cycles. This child mirrors
    that: CAS custody stat (lstat + no symlink), writer archive at the
    kernel_reader-pinned root mode with its paired object mode (0750/0440
    or 0700/0400), kernel store, then the kernel-stream audit and the
    compilation-row read. It performs no writes through the handle. Emits
    the distilled audit facts plus the registered row as one JSON
    document.
    """

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from aletheia.research_store.cas import FilesystemResearchArchive, ResearchArchiveError
    from aletheia.research_store.store import ResearchKernelStore

    kernel_reader = worker.kernel_reader
    if kernel_reader.cas_owner_uid == 0:
        _fail(
            "kernel CAS is owned by root; the audit mirror needs the driver "
            "identity the writer pin names"
        )
    try:
        # full drop, matching the sudo-launched driver this audit mirrors:
        # real/effective/saved all pin to the driver identity and the
        # supplementary group list empties, so the child cannot re-raise
        # root while executing the imported archive/DB surface.  Groups and
        # gid go first: each drop needs the privilege the previous line
        # still holds.
        os.setgroups([])
        os.setresgid(
            kernel_reader.cas_group_gid,
            kernel_reader.cas_group_gid,
            kernel_reader.cas_group_gid,
        )
        os.setresuid(
            kernel_reader.cas_owner_uid,
            kernel_reader.cas_owner_uid,
            kernel_reader.cas_owner_uid,
        )
    except OSError as exc:
        _fail(
            f"cannot drop to the driver identity "
            f"{kernel_reader.cas_owner_uid}:{kernel_reader.cas_group_gid}: {exc}"
        )

    cas = Path(kernel_reader.cas_root)
    try:
        if cas.resolve(strict=True) != cas:
            _fail(f"kernel CAS root {cas} is a symlink")
        metadata = os.lstat(cas)
    except OSError as exc:
        _fail(f"cannot stat the kernel CAS root: {exc}")
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != kernel_reader.cas_owner_uid
        or metadata.st_gid != kernel_reader.cas_group_gid
        or metadata.st_dev != kernel_reader.cas_device_id
        or metadata.st_ino != kernel_reader.cas_inode
        or stat.S_IMODE(metadata.st_mode) != kernel_reader.cas_directory_mode
    ):
        _fail("Kernel CAS custody drifted from the commissioned kernel_reader pin")

    try:
        # the writer-handle construction the runtime itself performs
        # (arl2_runtime._compose_archive): root mode from the commissioned
        # pin with its paired object mode; the child only reads through it
        archive = FilesystemResearchArchive(
            cas,
            max_object_bytes=kernel_reader.max_object_bytes,
            read_only=False,
            directory_mode=kernel_reader.cas_directory_mode,
            object_mode=0o440 if kernel_reader.cas_directory_mode == 0o750 else 0o400,
        )
    except ResearchArchiveError as exc:
        _fail(f"kernel archive refuses the mirrored writer composition: {exc}")
    try:
        after = os.lstat(cas)
    except OSError as exc:
        _fail(f"kernel CAS disappeared during the audit composition: {exc}")
    if (
        metadata.st_dev != after.st_dev
        or metadata.st_ino != after.st_ino
        or metadata.st_uid != after.st_uid
        or metadata.st_gid != after.st_gid
        or stat.S_IMODE(metadata.st_mode) != stat.S_IMODE(after.st_mode)
    ):
        _fail("kernel CAS custody changed during the audit composition")
    store = ResearchKernelStore(trust_root=kernel_reader.trust_root, archive=archive)

    from aletheia.research_kernel.reducer import ActionLifecycle
    from aletheia.observations.store import get_protocol_compilation_by_action

    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            audit = store.audit_in_session(session, quest_id)
            row = get_protocol_compilation_by_action(
                session, quest_id=quest_id, action_sha256=action_sha
            )
            session.commit()
    finally:
        engine.dispose()
    if row is None:
        _fail(f"no registered compilation for action {action_sha} on quest {quest_id}")

    graph = audit.state
    if (
        graph.terminal
        or not audit.events
        or len(audit.events) != len(audit.verified_snapshot_sha256s)
        or audit.events[-1].event_sha256 != graph.tail_event_sha256
        or audit.verified_snapshot_sha256s[-1] != graph.snapshot_sha256
    ):
        _fail("kernel audit fails its own stream invariants")
    action_states = [
        item for item in graph.actions if item.action_ref.object_sha256 == action_sha
    ]
    if len(action_states) != 1 or action_states[0].lifecycle is not ActionLifecycle.AUTHORIZED:
        _fail("the audited stream does not hold exactly one authorized instance of the action")
    action_state = action_states[0]
    proposed_events = [
        event for event in audit.events if event.event_sha256 == action_state.proposed_event_sha256
    ]
    authorized_events = [
        event for event in audit.events if event.event_sha256 == action_state.decided_event_sha256
    ]
    if len(proposed_events) != 1 or len(authorized_events) != 1:
        _fail("action proposal/authorization events are not unique in the audited stream")

    payload = {
        "snapshot_sha256": graph.snapshot_sha256,
        "scope_binding": audit.scope_binding.model_dump(mode="json"),
        "action_branch_id": action_state.branch_id,
        "proposed_event": proposed_events[0].model_dump(mode="json"),
        "authorized_event": authorized_events[0].model_dump(mode="json"),
        "request_json": row.request_json,
        "result_json": row.result_json,
        "compilation_sha256": row.compilation_sha256,
        "registered_at": _iso(row.registered_at),
    }
    out_path.write_bytes(json.dumps(payload, sort_keys=True).encode("utf-8"))


def _run_kernel_audit(worker, database_url: str, quest_id: str, action_sha: str) -> dict:
    """Fork the audit child at the driver identity and collect its JSON payload."""

    # dir is pinned to /tmp: mkstemp's default follows TMPDIR, which may be a
    # per-user 0700 root directory the dropped child cannot traverse back into
    descriptor, temp_name = tempfile.mkstemp(prefix="arl2-kernel-audit-", dir="/tmp")
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        os.chown(
            temp_path, worker.kernel_reader.cas_owner_uid, worker.kernel_reader.cas_group_gid
        )
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            pid = os.fork()
        except OSError as exc:
            _fail(f"cannot fork the kernel audit child: {exc}")
        if pid == 0:
            status = 1
            try:
                _kernel_audit_child(worker, database_url, quest_id, action_sha, temp_path)
                status = 0
            except SystemExit as exc:
                status = int(exc.code or 1)
            except BaseException:
                traceback.print_exc()
            finally:
                os._exit(status)
        _, wait_status = os.waitpid(pid, 0)
        if os.WIFSIGNALED(wait_status):
            # a signal death bypasses every Python-level handler, so no
            # diagnostic exists; name the signal instead of pointing at one
            _fail(
                f"kernel audit child died by signal {os.WTERMSIG(wait_status)}; "
                "a signal death prints no diagnostic"
            )
        if not os.WIFEXITED(wait_status) or os.WEXITSTATUS(wait_status) != 0:
            _fail("kernel audit child failed; see the diagnostic above")
        return _load_json(temp_path)
    finally:
        temp_path.unlink(missing_ok=True)


def _scan_registry_keys(root: Path, namespace: str) -> list[dict]:
    """List every entry payload of one namespace for uniqueness pre-scans."""

    namespace_root = root / namespace / "sha256"
    if not namespace_root.is_dir():
        return []
    entries = []
    for prefix in sorted(namespace_root.iterdir()):
        if not prefix.is_dir():
            continue
        for document in sorted(prefix.glob("*.json")):
            entries.append(_load_json(document))
    return entries


def _scan_registry_digests(root: Path, namespace: str) -> set[str]:
    """Content-addressed entry digests of one namespace (the stored file names)."""

    namespace_root = root / namespace / "sha256"
    if not namespace_root.is_dir():
        return set()
    return {document.stem for document in namespace_root.glob("*/*.json")}


def _append_registry_entry(
    root: Path,
    namespace: str,
    payload: bytes,
    signature: bytes,
    digest: str,
    filesystem_pin,
) -> None:
    """Thaw-free append of one entry with exact custody restoration.

    Root bypasses directory permissions, so no chmod is needed to write; the
    obligation is to leave the touched path in the pinned state afterwards:
    files owned by the pinned uid at the pinned file mode, every touched
    directory at the pinned directory mode.
    """

    driver_gid = os.stat(root).st_gid
    namespace_dir = root / namespace
    sha_dir = namespace_dir / "sha256"
    prefix_dir = sha_dir / digest[:2]
    document_path = prefix_dir / f"{digest}.json"
    signature_path = prefix_dir / f"{digest}.sig"
    if document_path.exists() or signature_path.exists():
        _fail(f"registry already holds digest {digest[:12]}... in {namespace}")
    created: list[tuple[Path, bool]] = []
    try:
        # parents=True may silently materialize namespace_dir/sha_dir; track
        # every level this call actually creates so the failure path restores
        # its custody too (otherwise it stays root-owned at mkdir's default)
        for directory in (namespace_dir, sha_dir, prefix_dir):
            if not directory.exists():
                directory.mkdir(mode=0o755)
                created.append((directory, False))
        prefix_dir.mkdir(exist_ok=True)
        for path, content in ((document_path, payload), (signature_path, signature)):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created.append((path, True))
            try:
                os.write(descriptor, content)
            finally:
                os.close(descriptor)
            os.chown(path, filesystem_pin.owner_uid, driver_gid)
            os.chmod(path, filesystem_pin.file_mode)
        for directory in (prefix_dir, sha_dir, namespace_dir):
            os.chown(directory, filesystem_pin.owner_uid, driver_gid)
            os.chmod(directory, filesystem_pin.directory_mode)
    except OSError as exc:
        # Best effort: leave every created path in pinned custody so the
        # registry still instantiates; the half-written pair stays behind for
        # manual root removal (documented in the module docstring).
        for path, is_file in created:
            try:
                os.chown(path, filesystem_pin.owner_uid, driver_gid)
                os.chmod(
                    path,
                    filesystem_pin.file_mode if is_file else filesystem_pin.directory_mode,
                )
            except OSError:
                pass
        _fail(
            f"registry append failed for digest {digest[:12]}... in {namespace}: {exc}; "
            "a half-written .json/.sig pair may need manual root removal before the "
            "fresh-id re-run"
        )


def _load_rate_card(root: Path, rate_card_sha256: str):
    from aletheia.execution.authority_contracts import ExecutionRateCard

    path = root / "rate_cards" / "sha256" / rate_card_sha256[:2] / f"{rate_card_sha256}.json"
    card = ExecutionRateCard.model_validate(_load_json(path))
    if card.rate_card_sha256 != rate_card_sha256:
        _fail("indexed rate card file does not carry its pinned digest")
    return card


def _run_sea(args, state: dict, state_path: Path) -> int:
    from aletheia.execution.authority_contracts import (
        SourceBudgetAuthorization,
        SourceBudgetProjection,
        execution_cost_quote_signature_message,
        source_budget_projection_signature_message,
        source_budget_signature_message,
    )
    from aletheia.execution.authority_registry import (
        CompositeExecutionAuthorityResolver,
        ExactExecutionCostQuoteRegistry,
        SourceBudgetProjectionRegistry,
    )
    from aletheia.execution.qualification_custody import QualificationPreAdmissionCustodyConfig
    from aletheia.execution.runtime_contracts import (
        BudgetAuthorization,
        EngineeringQualificationBundle,
        ExecutionCostQuote,
        ExecutionIntent,
        QualificationAuthorityVerifier,
        issue_engineering_qualification_grant,
        verify_engineering_qualification,
    )
    from aletheia.execution.schemas import (
        InfrastructureAttempt,
        ScientificReplicateSlot,
        canonical_json_bytes as execution_canonical_bytes,
    )
    from aletheia.observations.scientific_bridge import (
        ObservationAdmissionPolicy,
        ScientificActionProtocolBinding,
        ScientificObservationArtifactBinding,
        ScientificObservationOutcome,
        ScientificOutcomeBinMapping,
    )
    from aletheia.protocols.compiler import ProtocolCompilationRequest
    from aletheia.protocols.schemas import ProtocolCompilationResult, ProtocolStepRole
    from aletheia.research_controller.action_proposals import SubmittedActionProposal
    from aletheia.research_controller.execution_authorization_service import (
        FrozenScientificExecutionAuthorizationCatalog,
        FrozenScientificExecutionAuthorizationTemplate,
    )
    from aletheia.research_kernel.commands import ResearchScopeBinding
    from aletheia.research_kernel.schemas import ResearchEvent

    if os.geteuid() != 0:
        _fail("--mode sea must run as root (registry thaw/chown/chmod)")

    database_url = args.database_url
    if _sha256_bytes(database_url.encode("utf-8")) != state["database_url_sha256"]:
        _fail("database URL sha differs from the commissioned value")

    worker = _load_worker_config(state)
    reader = worker.terminal_reader
    custody = QualificationPreAdmissionCustodyConfig.model_validate(
        state["qualification"]["custody"]
    )
    qualification_pin = custody.qualification_authority_pin
    pricing_pin = reader.pricing_authority_pin
    source_pin = reader.source_budget_authority_pin

    submission = SubmittedActionProposal.model_validate(_load_json(Path(args.submission)))
    action = submission.action
    action_sha = action.object_sha256
    quest_id = action.quest_id

    # the kernel audit + compilation-row read run as the CAS owner uid in a
    # forked child; the payload carries every audited fact this mode needs
    facts = _run_kernel_audit(worker, database_url, quest_id, action_sha)
    snapshot_sha256 = facts["snapshot_sha256"]
    scope_binding = ResearchScopeBinding.model_validate(facts["scope_binding"])
    action_branch_id = facts["action_branch_id"]
    proposed_event = ResearchEvent.model_validate(facts["proposed_event"])
    authorized_event = ResearchEvent.model_validate(facts["authorized_event"])
    compilation_sha256 = facts["compilation_sha256"]
    registered_at = _parse_iso(facts["registered_at"], "compilation registered_at")

    request = ProtocolCompilationRequest.model_validate(facts["request_json"])
    result = ProtocolCompilationResult.model_validate(facts["result_json"])
    if result.work_order is None:
        _fail("the registered compilation produced no work order")
    work_order = result.work_order
    from aletheia.protocols.compiler import verify_compilation

    verify_compilation(request, result)
    protocol = request.protocol

    # the compiled graph scope must be the audited one, field for field
    if (
        protocol.graph_scope.scope_binding != scope_binding
        or protocol.graph_scope.branch_id != action_branch_id
        or protocol.graph_scope.question_ref != action.question_ref
        or protocol.graph_scope.graph_snapshot_sha256 != snapshot_sha256
    ):
        _fail("compiled protocol graph scope differs from the audited kernel stream")
    if submission.target_branch_id != action_branch_id:
        _fail("spool submission branch differs from the audited action branch")
    if not authorized_event.committed_at <= protocol.authored_at <= registered_at:
        _fail("compiled protocol authorship escapes the authorized/registered window")

    # node + slot projection
    if args.node_id:
        nodes = [node for node in work_order.nodes if node.node_id == args.node_id]
    else:
        nodes = [
            node for node in work_order.nodes if node.role is ProtocolStepRole.SCIENTIFIC_EXECUTOR
        ]
    if len(nodes) != 1:
        _fail("work order does not hold exactly one scientific-executor node (pass --node-id)")
    node = nodes[0]
    # contradiction #18: executor input ports are admissible when they are
    # protocol-level inputs (no WorkOrder producer); lineage edges stay
    # first-round-refused exactly like the compiler's binding verifier treats
    # them — continuation rounds carry those, not the commissioning intent
    produced_ports = _produced_output_port_ids(work_order)
    lineage_inputs = sorted(set(node.input_port_ids) & produced_ports)
    if lineage_inputs:
        _fail(
            f"node {node.node_id} declares work-order lineage inputs {lineage_inputs}; "
            "first-round commissioning binds only protocol-level inputs"
        )
    from aletheia.execution.schemas import ExecutionEffectClass, NetworkPolicy

    # contradiction #15: an external bridge action kind is admissible when the
    # frozen catalog's EXTERNAL class carries it; one-shot external effects and
    # non-NONE networks stay outside engineering qualification
    if (
        node.effect_class is not ExecutionEffectClass.REPLAY_SAFE
        or node.resource_request.network_policy is not NetworkPolicy.NONE
    ):
        _fail(
            f"node {node.node_id} is not replay-safe; the qualification bundle forbids "
            "non-replay-safe effect classes and non-NONE network policies"
        )
    slot = ScientificReplicateSlot(
        quest_id=work_order.quest_id,
        protocol_sha256=work_order.protocol_sha256,
        work_order_id=work_order.work_order_id,
        work_order_node_id=node.node_id,
        work_order_node_sha256=node.node_sha256,
        slot_count=node.scientific_replicate_count,
        slot_index=1,
        replicate_kind=node.replicate_kind,
        preregistration_sha256=node.replicate_preregistration_sha256,
        randomization_seed_sha256=node.replicate_seed_sha256s[0],
        independent_site_required=node.independent_site_required,
    )

    # source budget sidecar re-read and pin check
    sidecar_path = Path(args.source_budget_sidecar)
    sidecar_bytes = _read_bytes(sidecar_path)
    source = SourceBudgetAuthorization.model_validate(json.loads(sidecar_bytes))
    if (
        _sha256_bytes(sidecar_bytes) != source.source_budget_authorization_sha256
        or source.source_budget_authorization_sha256
        != protocol.resource_budget.budget_authorization_sha256
    ):
        _fail("source-budget sidecar does not close over the compiled protocol budget pin")
    if (
        source.quest_id != quest_id
        or source.currency_code != protocol.resource_budget.currency_code
        or source.maximum_cost_microunits != protocol.resource_budget.maximum_cost_microunits
        or source.deadline != protocol.resource_budget.deadline
    ):
        _fail("source budget numbers differ from the compiled resource budget")

    # timing lattice
    now = datetime.now(timezone.utc)
    state_valid_from = _parse_iso(state["valid_from"], "state valid_from")
    state_expires_at = _parse_iso(state["expires_at"], "state expires_at")
    _assert_window("commissioning key", state_valid_from, state_expires_at, now)
    if now <= registered_at:
        _fail("grant issuance must follow the compilation registration")

    protocol_deadline = protocol.resource_budget.deadline
    intent_authorized_at = source.authorized_at
    intent_deadline = protocol_deadline - timedelta(seconds=INTENT_DEADLINE_MARGIN_SECONDS)
    if intent_deadline <= now:
        _fail("protocol deadline leaves no room for the intent window")
    # bound the observation-admission deadline locally: the catalog dry-run
    # would reject a deadline past a bridge pin only AFTER the registry
    # appends burned their uniqueness keys
    if protocol_deadline > state_expires_at:
        _fail("protocol deadline outlives the deployment state window")
    from aletheia.observations.scientific_bridge import ScientificBridgeAuthorityPin

    for role in ("validator", "admission", "execution"):
        pin = ScientificBridgeAuthorityPin.model_validate(state["bridge"][role])
        if not pin.active_at(now):
            _fail(
                f"{role} bridge authority is inactive at grant time (active from "
                f"{_iso(pin.valid_from)} to {_iso(pin.active_until)})"
            )
        bridge_limit = _bridge_active_until(state, role)
        if protocol_deadline > bridge_limit:
            _fail(
                f"protocol deadline {_iso(protocol_deadline)} outlives the {role} "
                f"bridge authority (active until {_iso(bridge_limit)}); stage an "
                "earlier deadline"
            )
    admission_frozen_at = _parse_iso(state["prepared_at"], "state prepared_at")
    if admission_frozen_at > protocol.authored_at:
        _fail(
            "admission policy frozen_at is later than the protocol's authored_at; "
            "the admission window would open after the protocol was authored"
        )

    registry_root = Path(reader.authority_registry_root)
    filesystem_pin = reader.authority_registry_filesystem_pin
    card = _load_rate_card(registry_root, state["qualification"]["rate_card_sha256"])

    lease_seconds = node.resource_request.wall_time_seconds
    accepted_classes = tuple(sorted(node.resource_request.accepted_resource_class_ids))
    lines = [
        line
        for line in card.lines
        if line.accepted_resource_class_ids == accepted_classes
        and line.currency_code == protocol.resource_budget.currency_code
    ]
    if len(lines) != 1:
        _fail("deployed rate card has no unique line for the intent resource envelope")
    line = lines[0]
    if lease_seconds > line.maximum_lease_seconds:
        _fail("intent wall time exceeds the rate-card lease cap")
    if not card.active_at(now) or not pricing_pin.active_at(now):
        _fail("rate card or pricing authority is inactive at quote time")

    # ---- protocol-input admission (contradiction #18 remedy, PI-approved
    # design B, 2026-09-22) ---------------------------------------------
    # Every executor input port without a WorkOrder producer is bound to a
    # verified receipt minted through the deployment artifact store's real
    # custody chain (quarantine -> central rehash -> CAS -> manifest -> AVR)
    # under a DEDICATED admission slot and attempt. The dedicated identity is
    # load-bearing: a terminal-archive row filed later under the admission
    # attempt would make every fresh resolution of the bound receipt fail
    # producer-lineage validation, so the admission slot must never be the
    # round slot. Admission writes only content-addressed store objects (no
    # registry uniqueness keys burn), and re-runs read the published winner.
    protocol_ports = {item.port_id: item for item in protocol.data_ports}
    unproduced_inputs = sorted(set(node.input_port_ids) - produced_ports)
    declared_inputs = {}
    for item in args.protocol_input:
        port_id, separator, raw_path = item.partition("=")
        if not separator or not port_id or not raw_path:
            _fail(f"--protocol-input entries must look like PORT=PATH, got {item!r}")
        if port_id in declared_inputs:
            _fail(f"duplicate --protocol-input for port {port_id!r}")
        declared_inputs[port_id] = Path(raw_path)
    missing_inputs = sorted(set(unproduced_inputs) - set(declared_inputs))
    unknown_inputs = sorted(set(declared_inputs) - set(unproduced_inputs))
    if missing_inputs or unknown_inputs:
        _fail(
            "--protocol-input declarations do not match the executor's protocol-level "
            f"input ports: missing {missing_inputs}, not protocol-level {unknown_inputs}"
        )
    input_bindings = []
    admission_records = []
    artifact_store = None
    if unproduced_inputs:
        from aletheia.execution.artifact_store import LocalArtifactStore
        from aletheia.execution.schemas import InputArtifactBinding
        from aletheia.protocols.schemas import ProtocolPortDirection

        manifests_by_sha = {
            item.manifest_sha256: item for item in request.capability_catalog.manifests
        }
        node_manifest = manifests_by_sha.get(node.capability_manifest_sha256)
        if node_manifest is None:
            _fail("executor capability manifest is absent from the frozen catalog")
        custody = state["qualification"]["custody"]
        artifact_store = LocalArtifactStore(
            Path(custody["artifact_store_root"]),
            verifier_principal_id=custody["artifact_verifier_principal_id"],
            object_store_id=custody["artifact_object_store_id"],
        )
    for port_id in unproduced_inputs:
        port = protocol_ports.get(port_id)
        if port is None or port.direction is not ProtocolPortDirection.INPUT:
            _fail(f"executor input port {port_id!r} is not a protocol-level input port")
        requirement = _protocol_input_requirement(
            port=port,
            retention_policy_sha256=node_manifest.license_egress.retention_policy_sha256,
            max_bytes=args.protocol_input_max_bytes,
        )
        content = _read_bytes(declared_inputs[port_id])
        if len(content) > args.protocol_input_max_bytes:
            _fail(
                f"protocol input {port_id} exceeds --protocol-input-max-bytes "
                f"({len(content)} > {args.protocol_input_max_bytes})"
            )
        admission_slot = ScientificReplicateSlot(
            quest_id=work_order.quest_id,
            protocol_sha256=work_order.protocol_sha256,
            work_order_id=work_order.work_order_id,
            work_order_node_id=node.node_id,
            work_order_node_sha256=node.node_sha256,
            slot_count=1,
            slot_index=1,
            replicate_kind=node.replicate_kind,
            preregistration_sha256=requirement.expected_artifact_sha256,
            randomization_seed_sha256=_sha256_bytes(content),
            independent_site_required=node.independent_site_required,
        )
        admission_intent = ExecutionIntent(
            quest_id=work_order.quest_id,
            protocol_sha256=work_order.protocol_sha256,
            work_order_id=work_order.work_order_id,
            work_order_sha256=work_order.work_order_sha256,
            work_order_node_id=node.node_id,
            work_order_node_sha256=node.node_sha256,
            capability_id=node.capability_id,
            capability_manifest_sha256=node.capability_manifest_sha256,
            external_action_kind=node.external_action_kind,
            resource_catalog_sha256=work_order.resource_catalog_sha256,
            resource_request=node.resource_request.model_copy(
                update={
                    "artifact_quota_bytes": max(
                        node.resource_request.artifact_quota_bytes, len(content)
                    )
                }
            ),
            retry_policy=node.retry_policy,
            replicate_slot=admission_slot,
            infrastructure_attempt=InfrastructureAttempt(
                replicate_slot_id=admission_slot.replicate_slot_id,
                attempt_number=1,
            ),
            input_artifact_bindings=(),
            expected_artifacts=(requirement,),
            environment_sha256=node.environment_sha256,
            command_sha256=node.command_sha256,
            execution_parameters_sha256=node.execution_parameters_sha256,
            effect_class=node.effect_class,
            authorized_at=now,
            deadline=intent_deadline,
        )
        receipt = artifact_store.admit_protocol_input(
            intent=admission_intent,
            requirement=requirement,
            content=content,
            produced_at=now,
        )
        input_bindings.append(
            InputArtifactBinding(
                input_port_id=port_id,
                source_kind="protocol_input",
                artifact_verified_receipt_sha256=receipt.verified_receipt_sha256,
            )
        )
        admission_records.append(
            {
                "port_id": port_id,
                "admission_replicate_slot_id": admission_slot.replicate_slot_id,
                "admission_infrastructure_attempt_id": (
                    admission_intent.infrastructure_attempt.infrastructure_attempt_id
                ),
                "artifact_verified_receipt_sha256": receipt.verified_receipt_sha256,
                "content_sha256": _sha256_bytes(content),
                "bytes": len(content),
            }
        )
    input_bindings = tuple(sorted(input_bindings, key=lambda item: item.input_port_id))
    bound_receipt_sha256s = tuple(
        sorted({item.artifact_verified_receipt_sha256 for item in input_bindings})
    )

    intent = ExecutionIntent(
        quest_id=work_order.quest_id,
        protocol_sha256=work_order.protocol_sha256,
        work_order_id=work_order.work_order_id,
        work_order_sha256=work_order.work_order_sha256,
        work_order_node_id=node.node_id,
        work_order_node_sha256=node.node_sha256,
        capability_id=node.capability_id,
        capability_manifest_sha256=node.capability_manifest_sha256,
        external_action_kind=node.external_action_kind,
        resource_catalog_sha256=work_order.resource_catalog_sha256,
        resource_request=node.resource_request,
        retry_policy=node.retry_policy,
        replicate_slot=slot,
        infrastructure_attempt=InfrastructureAttempt(
            replicate_slot_id=slot.replicate_slot_id,
            attempt_number=1,
        ),
        input_artifact_bindings=input_bindings,
        expected_artifacts=node.expected_artifacts,
        environment_sha256=node.environment_sha256,
        command_sha256=node.command_sha256,
        execution_parameters_sha256=node.execution_parameters_sha256,
        effect_class=node.effect_class,
        authorized_at=intent_authorized_at,
        deadline=intent_deadline,
    )

    manifest_sha256 = reader.node_authorities[0].manifest.manifest_sha256
    node_id = reader.node_authorities[0].manifest.node_id
    quote_fields = dict(
        quest_id=work_order.quest_id,
        protocol_sha256=work_order.protocol_sha256,
        work_order_sha256=work_order.work_order_sha256,
        intent_sha256=intent.intent_sha256,
        execution_id=intent.execution_id,
        infrastructure_attempt_id=intent.infrastructure_attempt.infrastructure_attempt_id,
        accepted_resource_class_ids=intent.resource_request.accepted_resource_class_ids,
    )
    external_bridge_active_until = None
    if node.external_action_kind is not None:
        # external placement mode: exactly one frozen EXTERNAL class carrying
        # the node's action kind (the bundle validator profile)
        from aletheia.execution.schemas import ResourceKind

        external_classes = [
            item
            for item in request.resource_catalog.resource_classes
            if item.kind is ResourceKind.EXTERNAL
            and node.external_action_kind in item.external_action_kinds
            and item.resource_class_id
            in intent.resource_request.accepted_resource_class_ids
        ]
        if len(external_classes) != 1:
            _fail(
                "the frozen resource catalog does not hold exactly one accepted "
                f"EXTERNAL class carrying action kind {node.external_action_kind}"
            )
        # the quote's uniqueness key burns on append; admission re-checks the
        # bridge pin, so a window it cannot survive must fail HERE (contradiction
        # #15 review: fold the bridge authority's active window into expiry)
        external_bridge_active_until = _external_bridge_active_until(state)
        if external_bridge_active_until is None:
            _fail(
                "external placement quotes need the deployment's external bridge "
                "authority pin; re-author the deployment carrying it"
            )
        if external_bridge_active_until <= now:
            _fail("external bridge authority pin is no longer active at quote time")
        placement = dict(
            permitted_node_manifest_sha256s=(),
            selected_node_manifest_sha256=None,
            selected_resource_ids=(),
            selected_external_resource_class_id=external_classes[0].resource_class_id,
            selected_external_resource_class_key=external_classes[0].class_key,
        )
    else:
        placement = dict(
            permitted_node_manifest_sha256s=(manifest_sha256,),
            selected_node_manifest_sha256=manifest_sha256,
            selected_resource_ids=(node_id,),
            selected_external_resource_class_id=None,
            selected_external_resource_class_key=None,
        )
    quote_window = [
        source.expires_at,
        intent_deadline,
        card.active_until,
        pricing_pin.active_until,
    ]
    if external_bridge_active_until is not None:
        quote_window.append(external_bridge_active_until)
    quote = ExecutionCostQuote(
        **quote_fields,
        **placement,
        currency_code=protocol.resource_budget.currency_code,
        rate_card_sha256=card.rate_card_sha256,
        fixed_charge_microunits=line.fixed_charge_microunits,
        charge_per_second_microunits=line.charge_per_second_microunits,
        maximum_lease_seconds=lease_seconds,
        maximum_charge_microunits=(
            line.fixed_charge_microunits + line.charge_per_second_microunits * lease_seconds
        ),
        pricing_policy_sha256=pricing_pin.policy_sha256,
        quoted_by_principal_id=pricing_pin.principal_id,
        quoted_at=now,
        expires_at=min(quote_window),
    )
    if quote.expires_at <= now:
        _fail("derived quote expiry is not in the future; check the source window and pins")

    budget_authorization = BudgetAuthorization(
        quest_id=quest_id,
        protocol_sha256=work_order.protocol_sha256,
        work_order_sha256=work_order.work_order_sha256,
        resource_budget_sha256=protocol.resource_budget.resource_budget_sha256,
        source_budget_authorization_sha256=source.source_budget_authorization_sha256,
        currency_code=source.currency_code,
        maximum_cost_microunits=source.maximum_cost_microunits,
        deadline=source.deadline,
        authorized_by_principal_id=source.authorized_by_principal_id,
        authorized_at=source.authorized_at,
        expires_at=source.expires_at,
    )

    bundle = EngineeringQualificationBundle(
        compilation_request=request,
        compilation_result=result,
        work_order=work_order,
        intent=intent,
        prior_execution_receipt=None,
        input_artifact_verified_receipt_sha256s=bound_receipt_sha256s,
        budget_authorization=budget_authorization,
        cost_quote=quote,
    )

    # ---- registry append: pre-scan, sign, append, restore, re-instantiate --
    quote_registry = ExactExecutionCostQuoteRegistry(
        registry_root, filesystem_pin=filesystem_pin, pricing_authority_pin=pricing_pin
    )
    budget_registry = SourceBudgetProjectionRegistry(
        registry_root,
        filesystem_pin=filesystem_pin,
        source_budget_authority_pin=source_pin,
    )

    attempt_id = intent.infrastructure_attempt.infrastructure_attempt_id
    for existing in _scan_registry_keys(registry_root, "execution_cost_quotes"):
        if existing.get("infrastructure_attempt_id") == attempt_id:
            _fail("registry already holds a quote for this infrastructure attempt")
    source_entries = _scan_registry_keys(registry_root, "source_budgets")
    for existing in source_entries:
        if existing.get("source_budget_id") == source.source_budget_id:
            _fail("registry already holds this source budget id; pass a fresh --source-budget-id")
    # a serialized source_budgets document carries no self-hash field (the
    # model self-hash is a computed property), so the live form of the
    # source-authorization scan compares the content-addressed file names,
    # which equal each stored entry's payload digest
    if source.source_budget_authorization_sha256 in _scan_registry_digests(
        registry_root, "source_budgets"
    ):
        _fail(
            "registry already holds this source authorization under another "
            "source_budget_id; adjust the protocol deadline and pass a fresh "
            "--source-budget-id"
        )
    projection_entries = _scan_registry_keys(registry_root, "source_budget_projections")
    if (
        budget_authorization.authorization_sha256
        in {item.get("budget_authorization_sha256") for item in projection_entries}
        or source.source_budget_authorization_sha256
        in {item.get("source_budget_authorization_sha256") for item in projection_entries}
        or budget_authorization.resource_budget_sha256
        in {
            (item.get("budget_authorization") or {}).get("resource_budget_sha256")
            for item in projection_entries
        }
    ):
        _fail(
            "registry already projects this budget or resource budget; adjust the "
            "protocol deadline and pass a fresh --source-budget-id"
        )

    projection = SourceBudgetProjection(
        source_budget_authorization_sha256=source.source_budget_authorization_sha256,
        source_authorization_policy_sha256=source_pin.policy_sha256,
        budget_authorization_sha256=budget_authorization.authorization_sha256,
        budget_authorization=budget_authorization,
        projected_by_principal_id=source_pin.principal_id,
        source_authority_key_id=source_pin.key_id,
        projected_at=now,
    )

    working = state_path.parent.parent
    pricing_key = working / "keys" / "qualification" / "pricing.key"
    source_key = working / "keys" / "qualification" / "source_budget.key"
    # the qualifier signing key is authored under the ACTIVATION working root
    # (keys/qualifier/qualifier.key); nothing copies it into the deployment
    # tree, so point --qualifier-key there when the roots differ
    qualifier_key = (
        Path(args.qualifier_key) if args.qualifier_key else working / "keys" / "qualifier" / "qualifier.key"
    )
    if _derive_public_hex(pricing_key) != pricing_pin.public_key_ed25519_hex:
        _fail(f"{pricing_key} does not derive the pinned pricing public key")
    if _derive_public_hex(source_key) != source_pin.public_key_ed25519_hex:
        _fail(f"{source_key} does not derive the pinned source-budget public key")
    if _derive_public_hex(qualifier_key) != qualification_pin.public_key_ed25519_hex:
        _fail(f"{qualifier_key} does not derive the pinned qualification public key")

    quote_payload = execution_canonical_bytes(quote)
    source_payload = execution_canonical_bytes(source)
    projection_payload = execution_canonical_bytes(projection)
    for payload, digest, model_hash in (
        (quote_payload, _sha256_bytes(quote_payload), quote.quote_sha256),
        (source_payload, _sha256_bytes(source_payload), source.source_budget_authorization_sha256),
        (
            projection_payload,
            _sha256_bytes(projection_payload),
            projection.projection_sha256,
        ),
    ):
        if digest != model_hash:
            _fail("canonical payload does not equal the model self-hash; refusing to append")

    # ---- hoisted pre-append validation -------------------------------------
    # Every operator-fixable rejection below needs nothing the appends
    # produce, so it runs before they burn the quote/projection/source
    # uniqueness keys; only grant issuance, template assembly, and the
    # registry self-checks must follow the appends.

    grant_authorized_at = now
    grant_expires_at = min(
        intent_deadline,
        source.expires_at,
        quote.expires_at,
        qualification_pin.active_until,
    )
    if grant_expires_at <= grant_authorized_at:
        _fail("derived grant window is empty; check the source window and pins")
    if grant_authorized_at + timedelta(seconds=lease_seconds) > grant_expires_at:
        _fail("quoted lease does not fit inside the derived grant window")

    binding = ScientificActionProtocolBinding(
        action=action,
        action_proposed_event=proposed_event,
        action_authorized_event=authorized_event,
        authorized_graph_snapshot_sha256=snapshot_sha256,
        compilation_request=request,
        compilation_result=result,
        compilation_receipt=result.receipt,
        work_order=work_order,
        work_order_node=node,
        replicate_slot=slot,
        bound_at=registered_at,
    )

    if len(node.observable_output_bindings) != 1:
        _fail("executor node does not carry exactly one observable output binding")
    output_binding = node.observable_output_bindings[0]
    observables = [
        item
        for item in protocol.observables
        if item.observable_sha256 == output_binding.observable_spec_sha256
    ]
    ports = [item for item in protocol.data_ports if item.port_id == output_binding.output_port_id]
    artifacts = [
        item for item in node.expected_artifacts if item.artifact_key == output_binding.output_port_id
    ]
    steps = [item for item in protocol.steps if item.step_id == node.protocol_step_id]
    if len(observables) != 1 or len(ports) != 1 or len(artifacts) != 1 or len(steps) != 1:
        _fail("observation artifact members do not resolve uniquely inside the protocol")
    artifact_binding = ScientificObservationArtifactBinding(
        work_order_node=node,
        protocol_step=steps[0],
        observable_output_binding=output_binding,
        observable=observables[0],
        data_port=ports[0],
        expected_artifact=artifacts[0],
        observation_namespace_sha256=args.observation_namespace_sha256,
        selection_campaign_sha256=args.selection_campaign_sha256,
        prediction_campaign_sha256=args.prediction_campaign_sha256,
        prediction_commitment_sha256=args.prediction_commitment_sha256,
    )

    bins = {}
    for item in args.outcome_bin_mapping:
        bin_id, _, outcome = item.partition("=")
        if not bin_id or not outcome:
            _fail(f"--outcome-bin-mapping entries must look like BIN=OUTCOME, got {item!r}")
        if outcome not in ("positive", "negative", "inconclusive"):
            _fail(f"unknown outcome {outcome!r} for bin {bin_id!r}")
        if bin_id in bins:
            _fail(f"duplicate outcome bin {bin_id!r}")
        bins[bin_id] = outcome
    if not bins:
        _fail("at least one --outcome-bin-mapping is required")
    validator_manifest_sha = state["bindings"]["independent_validation"]["json"][
        "service_manifest_sha256"
    ]
    validator_policy_sha = state["bridge"]["validator"]["policy_sha256"]
    admission_policy = ObservationAdmissionPolicy(
        policy_id=args.admission_policy_id
        or f"arl2-admission.{quest_id}.r{args.round_index}",
        validator_manifest_sha256=validator_manifest_sha,
        observation_validation_policy_sha256=validator_policy_sha,
        analysis_outcome_space_sha256=protocol.analysis_plan.outcome_space_sha256,
        outcome_bin_mappings=tuple(
            ScientificOutcomeBinMapping(
                outcome_bin_id=bin_id,
                outcome=ScientificObservationOutcome(outcome),
            )
            for bin_id in sorted(bins)
        ),
        frozen_at=_parse_iso(state["prepared_at"], "state prepared_at"),
    )

    sea_authorized_at = grant_authorized_at
    sea_expires_at = min(
        grant_expires_at,
        _bridge_active_until(state, "validator"),
        _bridge_active_until(state, "admission"),
        _bridge_active_until(state, "execution"),
    )
    if sea_expires_at <= sea_authorized_at:
        _fail("derived SEA window is empty; a bridge pin expires too early")

    # the catalog dry-run reads the deployed issuer source and the three
    # bridge pins; both are deployment state, independent of the registry
    release = Path(args.release_root).resolve(strict=True)
    issuer_source = release / "aletheia" / "research_controller" / "execution_authorization_service.py"
    issuer_sha = _sha256_bytes(_read_bytes(issuer_source))
    execution_bridge_pin = ScientificBridgeAuthorityPin.model_validate(state["bridge"]["execution"])
    validator_bridge_pin = ScientificBridgeAuthorityPin.model_validate(state["bridge"]["validator"])
    admission_bridge_pin = ScientificBridgeAuthorityPin.model_validate(state["bridge"]["admission"])

    output = Path(args.output)
    if output.exists():
        _fail(f"output {output} already exists; the template write is write-once")

    _append_registry_entry(
        registry_root,
        "source_budgets",
        source_payload,
        _sign_raw(source_key, source_budget_signature_message(source)),
        source.source_budget_authorization_sha256,
        filesystem_pin,
    )
    _append_registry_entry(
        registry_root,
        "source_budget_projections",
        projection_payload,
        _sign_raw(source_key, source_budget_projection_signature_message(projection)),
        projection.projection_sha256,
        filesystem_pin,
    )
    _append_registry_entry(
        registry_root,
        "execution_cost_quotes",
        quote_payload,
        _sign_raw(pricing_key, execution_cost_quote_signature_message(quote)),
        quote.quote_sha256,
        filesystem_pin,
    )

    # re-instantiation is the append self-check: construction re-validates
    # every entry's custody and authority, including the three just written
    quote_registry = ExactExecutionCostQuoteRegistry(
        registry_root, filesystem_pin=filesystem_pin, pricing_authority_pin=pricing_pin
    )
    budget_registry = SourceBudgetProjectionRegistry(
        registry_root,
        filesystem_pin=filesystem_pin,
        source_budget_authority_pin=source_pin,
    )
    resolver = CompositeExecutionAuthorityResolver(
        quote_registry=quote_registry,
        budget_registry=budget_registry,
        execution_receipt_resolver=_FailClosedReceiptResolver(),
    )
    if resolver.resolve_execution_cost_quote(
        cost_quote_sha256=quote.quote_sha256, observed_at=now
    ) != quote:
        _fail("re-instantiated registry does not resolve the appended quote")
    budget_resolution = resolver.resolve_budget_authorization(
        source_budget_authorization_sha256=source.source_budget_authorization_sha256,
        observed_at=now,
    )
    if (
        budget_resolution is None
        or budget_resolution.budget_authorization != budget_authorization
    ):
        _fail("re-instantiated registry does not resolve the appended projection")

    # ---- grant ------------------------------------------------------------
    # With input bindings the grant's custody resolution runs for real against
    # the deployment artifact store: the resolver freshly rehashes each bound
    # AVR/manifest/CAS closure. The terminal-archive stand-in lists nothing —
    # correct offline, because admission attempts never gain terminal rows.
    if input_bindings:
        from aletheia.execution.input_resolver import LocalVerifiedInputArtifactResolver

        grant_artifact_resolver = LocalVerifiedInputArtifactResolver(
            artifact_store=artifact_store,
            terminal_receipt_archive=_EmptyTerminalArchive(),
        )
    else:
        grant_artifact_resolver = _FailClosedArtifactResolver()
    grant = issue_engineering_qualification_grant(
        bundle,
        pin=qualification_pin,
        artifact_resolver=grant_artifact_resolver,
        authority_resolver=resolver,
        private_key=_read_bytes(qualifier_key),
        authorized_at=grant_authorized_at,
        expires_at=grant_expires_at,
    )
    verifier = QualificationAuthorityVerifier(qualification_pin)
    verifier.verify_signature(grant, observed_at=now)
    verify_engineering_qualification(
        bundle=bundle,
        grant=grant,
        authority=verifier,
        artifact_resolver=grant_artifact_resolver,
        authority_resolver=resolver,
        observed_at=now,
    )

    # ---- template, catalog dry-run, output ---------------------------------
    template = FrozenScientificExecutionAuthorizationTemplate(
        action_sha256=action_sha,
        compilation_sha256=compilation_sha256,
        action_protocol_binding=binding,
        qualification_bundle=bundle,
        qualification_grant=grant,
        validator_manifest_sha256=validator_manifest_sha,
        observation_validation_policy_sha256=validator_policy_sha,
        admission_policy=admission_policy,
        scientific_observation_artifact_binding=artifact_binding,
        authorized_at=sea_authorized_at,
        expires_at=sea_expires_at,
        observation_admission_deadline=protocol_deadline,
    )

    # catalog dry-run: construction re-derives the full SEA message per
    # template and runs the closure against the deployment pins
    catalog = FrozenScientificExecutionAuthorizationCatalog(
        issuer_implementation_sha256=issuer_sha,
        qualification_authority_pin=qualification_pin,
        execution_authority_pin=execution_bridge_pin,
        validator_authority_pin=validator_bridge_pin,
        admission_authority_pin=admission_bridge_pin,
        templates=(template,),
    )

    from aletheia.research_kernel.schemas import canonical_json_bytes

    _write_new_file(output, canonical_json_bytes(template), mode=0o644)

    summary = {
        "mode": "sea",
        "action_sha256": action_sha,
        "quest_id": quest_id,
        "compilation_sha256": compilation_sha256,
        "template_sha256": template.template_sha256,
        "catalog_sha256": catalog.catalog_sha256,
        "scientific_slot_id": binding.scientific_slot_id,
        "quote_sha256": quote.quote_sha256,
        "source_budget_authorization_sha256": source.source_budget_authorization_sha256,
        "projection_sha256": projection.projection_sha256,
        "protocol_input_admissions": admission_records,
        "grant_expires_at": _iso(grant_expires_at),
        "sea_expires_at": _iso(sea_expires_at),
        "observation_admission_deadline": _iso(protocol_deadline),
        "output": str(output),
    }
    print("SEA_TEMPLATE " + json.dumps(summary, sort_keys=True))
    return 0


def _bridge_active_until(state: dict, role: str) -> datetime:
    pin = state["bridge"][role]
    expires_at = _parse_iso(pin["expires_at"], f"bridge {role} expires_at")
    revoked_at = pin.get("revoked_at")
    if revoked_at is None:
        return expires_at
    return min(expires_at, _parse_iso(revoked_at, f"bridge {role} revoked_at"))


def _external_bridge_active_until(state: dict) -> datetime | None:
    """The deployment's external bridge admission pin window, if authored."""

    pin = state.get("qualification", {}).get("external_bridge_pin")
    if not pin:
        return None
    expires_at = _parse_iso(pin["expires_at"], "external bridge expires_at")
    revoked_at = pin.get("revoked_at")
    if revoked_at is None:
        return expires_at
    return min(expires_at, _parse_iso(revoked_at, "external bridge revoked_at"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Author the per-round ARL-2 provider template and/or SEA template."
    )
    parser.add_argument("--mode", required=True, choices=("provider", "sea"))
    parser.add_argument("--deployment-state", required=True)
    parser.add_argument("--submission", required=True, help="spool SubmittedActionProposal JSON")
    parser.add_argument("--output", required=True)
    parser.add_argument("--round-index", type=int, default=1)
    parser.add_argument("--source-budget-id", help="default source-budget.<quest>.r<round>")
    parser.add_argument("--source-authorized-at", help="default: authorization commit + 1 s")
    parser.add_argument("--source-expires-at", help="provider mode: required budget expiry")
    parser.add_argument("--source-budget-sidecar", help="sea mode: sidecar written at PAUSE-1")
    parser.add_argument("--protocol-body", help="provider mode: staged ProtocolIR JSON")
    parser.add_argument("--graph-snapshot-sha256", help="provider mode: audited snapshot sha")
    parser.add_argument("--action-authorized-committed-at", help="provider mode: ISO instant")
    parser.add_argument("--capability-catalog", help="provider mode: pinned catalog file")
    parser.add_argument("--resource-catalog", help="provider mode: pinned catalog file")
    parser.add_argument("--author-principal-id", help="default: first allowed protocol author")
    parser.add_argument("--database-url", help="sea mode: commissioned database URL")
    parser.add_argument("--node-id", help="sea mode: override the executor node choice")
    parser.add_argument(
        "--protocol-input",
        action="append",
        default=[],
        metavar="PORT=PATH",
        help="sea mode: admit PORT's artifact from PATH as a verified protocol input",
    )
    parser.add_argument(
        "--protocol-input-max-bytes",
        type=int,
        default=8_388_608,
        help="sea mode: per-port admission cap in bytes (default 8 MiB)",
    )
    parser.add_argument("--observation-namespace-sha256", help="sea mode: F9 campaign record")
    parser.add_argument("--selection-campaign-sha256", help="sea mode: F9 campaign record")
    parser.add_argument("--prediction-campaign-sha256", help="sea mode: F9 campaign record")
    parser.add_argument("--prediction-commitment-sha256", help="sea mode: F9 campaign record")
    parser.add_argument(
        "--outcome-bin-mapping",
        action="append",
        default=[],
        metavar="BIN=OUTCOME",
        help="sea mode: repeatable; OUTCOME in positive/negative/inconclusive",
    )
    parser.add_argument("--admission-policy-id", help="sea mode: default arl2-admission.<quest>.r<n>")
    parser.add_argument(
        "--qualifier-key",
        help="sea mode: activation qualifier signing key "
        "(default: <deployment working>/keys/qualifier/qualifier.key; the key is "
        "authored under the ACTIVATION working root)",
    )
    parser.add_argument("--release-root", help="sea mode: checked-out release root")
    args = parser.parse_args()

    try:
        state_path = Path(args.deployment_state).resolve(strict=True)
    except OSError as exc:
        _fail(f"cannot resolve the deployment state path: {exc}")
    state = _load_state(state_path)

    if args.mode == "provider":
        for required in (
            args.protocol_body,
            args.graph_snapshot_sha256,
            args.action_authorized_committed_at,
            args.capability_catalog,
            args.resource_catalog,
            args.source_expires_at,
        ):
            if not required:
                _fail("provider mode requires --protocol-body, --graph-snapshot-sha256, "
                      "--action-authorized-committed-at, --capability-catalog, "
                      "--resource-catalog, --source-expires-at")
        return _run_provider(args, state, state_path)

    for required in (
        args.database_url,
        args.source_budget_sidecar,
        args.observation_namespace_sha256,
        args.selection_campaign_sha256,
        args.prediction_campaign_sha256,
        args.prediction_commitment_sha256,
        args.release_root,
    ):
        if not required:
            _fail("sea mode requires --database-url, --source-budget-sidecar, the four "
                  "campaign digests, and --release-root")
    return _run_sea(args, state, state_path)


if __name__ == "__main__":
    raise SystemExit(main())
