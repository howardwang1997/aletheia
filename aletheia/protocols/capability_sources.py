"""Fresh verification of independently signed capability qualification sources.

Trust and runtime inventories are supplied through byte-pinned deployment configuration.
These records qualify bounded engineering operations and confer no scientific authority."""

from __future__ import annotations
import ast
import hashlib
import json
from pathlib import Path
import os
from typing import Literal
from pydantic import Field, model_validator
from aletheia.research_kernel.schemas import KernelModel
import stat
from datetime import datetime, timezone
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from aletheia.protocols.capabilities import CalibrationMode, QualificationStatus, RuntimeKind
from aletheia.protocols.schemas import CapabilityAuditKind
from aletheia.protocols.typecheck import expected_capability_audit_policy_sha256
from aletheia.research_kernel.schemas import canonical_json_bytes, canonical_sha256


class CapabilitySourceVerificationError(ValueError):
    """A source, signature, runtime identity or authority binding is invalid."""


def _require(condition, message):
    if not condition:
        raise CapabilitySourceVerificationError(str(message))


RULE = {
    "schema_name": "aletheia.capability_engineering_source_rule.v1",
    "signed_audits_required": True,
    "distinct_auditor_and_qualifier_required": True,
    "retained_material_bytes_required": True,
    "retained_check_results_required": True,
    "runtime_source_required": True,
    "scientific_authority": False,
}


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def utc(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(
        result.tzinfo is not None and result.utcoffset() == timezone.utc.utcoffset(result),
        "capability audit source is invalid or differs from its binding",
    )
    return result


def definition_sha256(manifest):
    return canonical_sha256(
        manifest.model_dump(mode="json", exclude={"qualification", "frozen_at"})
    )


def fresh(path):
    """Read one stable regular file through a no-follow descriptor."""
    path = Path(path)
    try:
        if path.resolve(strict=True) != path or path.is_symlink():
            raise CapabilitySourceVerificationError("capability source traverses a symlink")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_mode & 0o022
                or (not 0 < before.st_size <= 64 * 1024**2)
            ):
                raise CapabilitySourceVerificationError("capability source custody is unsafe")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                payload = stream.read(before.st_size + 1)
            after = os.fstat(fd)
            linked = path.lstat()
        finally:
            os.close(fd)
    except OSError as exc:
        raise CapabilitySourceVerificationError("capability source is unavailable") from exc

    def identity(metadata):
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_mode,
            metadata.st_nlink,
        )

    if (
        identity(before) != identity(after)
        or identity(after) != identity(linked)
        or len(payload) != before.st_size
    ):
        raise CapabilitySourceVerificationError("capability source changed while reading")
    return payload


def source(root, digest):
    _require(
        len(digest) == 64 and all((c in "0123456789abcdef" for c in digest)),
        "capability source digest is invalid or differs from its binding",
    )
    root = Path(root)
    if root.resolve(strict=True) != root:
        raise CapabilitySourceVerificationError("capability source root traverses a symlink")
    path = root / "objects" / digest[:2] / digest
    _require(path.resolve(strict=True) == path, path)
    payload = fresh(path)
    _require(sha(payload) == digest, "retained capability source digest differs")
    return payload


def signed_record(root, digest, pin, expected_schema):
    payload = source(root, digest)
    record = json.loads(payload)
    _require(canonical_json_bytes(record) == payload, "capability record is not canonical")
    _require(
        set(record) == {"message", "signature_ed25519_hex"},
        "capability audit source is invalid or differs from its binding",
    )
    message = record["message"]
    _require(
        message["schema_name"] == expected_schema,
        "capability signed record is invalid or differs from its binding",
    )
    _require(
        message["principal_id"] == pin["principal_id"],
        "capability authority pin is invalid or differs from its binding",
    )
    _require(
        message["key_id"] == pin["key_id"],
        "capability authority pin is invalid or differs from its binding",
    )
    if message["scientific_authority"] is not False:
        raise CapabilitySourceVerificationError(
            "capability source verification failed: message['scientific_authority'] is False"
        )
    _require(
        utc(pin["valid_from"]) <= utc(message["issued_at"]) < utc(pin["expires_at"]),
        "capability authority pin is invalid or differs from its binding",
    )
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(pin["public_key_ed25519_hex"])).verify(
        bytes.fromhex(record["signature_ed25519_hex"]), canonical_json_bytes(message)
    )
    _require(
        len(message["materials"]) >= 2,
        "capability signed record is invalid or differs from its binding",
    )
    _require(
        message["materials"] == sorted(set(message["materials"])),
        "capability signed record is invalid or differs from its binding",
    )
    for material in message["materials"]:
        source(root, material)
    return message


def _verify_check_result(root, record):
    digest = record["check_result_sha256"]
    _require(digest in record["materials"], "audit does not retain its check result")
    payload = source(root, digest)
    result = json.loads(payload)
    _require(canonical_json_bytes(result) == payload, "capability check result is not canonical")
    _require(
        result["schema_name"] == "aletheia.capability_engineering_check.v1",
        "capability check result schema differs",
    )
    for name in ("definition_sha256", "audit_kind", "audit_policy_sha256"):
        _require(result[name] == record[name], "capability check result escaped its audit scope")
    _require(
        result["scientific_authority"] is False and result["result"] == "passed",
        "capability check did not pass within engineering scope",
    )
    _require(
        utc(result["checked_at"]) <= utc(record["issued_at"]), "capability audit predates its check"
    )
    checks = result["checks"]
    _require(0 < len(checks) <= 4096, "capability check list is empty or unbounded")
    names = [item["check_id"] for item in checks]
    _require(
        names == sorted(set(names)) and all(isinstance(n, str) and n for n in names),
        "capability check identifiers are not canonical",
    )
    for check in checks:
        _require(check["passed"] is True, "capability audit contains a failed check")
        inputs = check["source_sha256s"]
        _require(
            0 < len(inputs) <= 4096 and inputs == sorted(set(inputs)),
            "capability check lacks canonical source inputs",
        )
        for digest in inputs:
            source(root, digest)


def verify_capability_sources(request, *, root, trust, runtime_sources):
    """Close every selected operation over trusted signatures and real retained bytes.

    ``trust`` and ``runtime_sources`` must be frozen outside the request and
    byte-pinned by the commissioning or ARL0 gate input manifest.
    """
    _require(
        trust["schema_name"] == "aletheia.capability_engineering_source_trust.v1",
        "capability trust policy is invalid or differs from its binding",
    )
    _require(
        trust["rule_sha256"] == canonical_sha256(RULE),
        "capability trust policy is invalid or differs from its binding",
    )
    if trust["scientific_authority"] is not False:
        raise CapabilitySourceVerificationError(
            "capability source verification failed: trust['scientific_authority'] is False"
        )
    auditor, qualifier = (trust["auditor"], trust["qualifier"])
    _require(
        auditor["principal_id"] != qualifier["principal_id"],
        "capability qualifier independence is invalid or differs from its binding",
    )
    _require(
        auditor["key_id"] != qualifier["key_id"],
        "capability qualifier independence is invalid or differs from its binding",
    )
    _require(
        bytes.fromhex(auditor["public_key_ed25519_hex"])
        != bytes.fromhex(qualifier["public_key_ed25519_hex"]),
        "capability qualifier independence is invalid or differs from its binding",
    )
    for pin in (auditor, qualifier):
        public = bytes.fromhex(pin["public_key_ed25519_hex"])
        _require(
            len(public) == 32 and public.hex() == pin["public_key_ed25519_hex"],
            "capability authority pin is invalid or differs from its binding",
        )
        _require(
            pin["key_id"] == sha(public),
            "capability authority pin is invalid or differs from its binding",
        )
        _require(
            utc(pin["valid_from"]) <= request.protocol.authored_at < utc(pin["expires_at"]),
            "capability authority pin is invalid or differs from its binding",
        )
    manifests = {m.manifest_sha256: m for m in request.capability_catalog.manifests}
    subjects = {}
    selected = {step.capability_requirement.manifest_sha256 for step in request.protocol.steps}
    _require(set(runtime_sources) == selected, "runtime source inventory is not exhaustive")
    for step in request.protocol.steps:
        requirement = step.capability_requirement
        manifest = manifests[requirement.manifest_sha256]
        if manifest.qualification.status is not QualificationStatus.QUALIFIED:
            raise CapabilitySourceVerificationError(
                "capability source verification failed: manifest.qualification.status is QualificationStatus.QUALIFIED"
            )
        _require(
            manifest.qualification.qualification_rule_sha256 == canonical_sha256(RULE),
            "capability qualification is invalid or differs from its binding",
        )
        _require(
            manifest.qualification.qualified_by_principal_id == qualifier["principal_id"],
            "capability qualifier independence is invalid or differs from its binding",
        )
        _require(
            auditor["principal_id"]
            not in {
                manifest.frozen_by_principal_id,
                manifest.principal.executor_principal_id,
                request.protocol.authored_by_principal_id,
                *request.protocol.independence.executor_principal_ids,
                *request.protocol.independence.parser_principal_ids,
                *request.protocol.independence.validator_principal_ids,
                *request.protocol.independence.claim_approver_principal_ids,
            },
            "capability auditor independence is invalid or differs from its binding",
        )
        subject = definition_sha256(manifest)
        runtime = runtime_sources[manifest.manifest_sha256]
        _require(
            runtime["adapter_ref"] == manifest.runtime.adapter_ref,
            "capability runtime identity is invalid or differs from its binding",
        )
        _require(
            runtime["implementation_sha256"] == manifest.runtime.implementation_sha256,
            "capability runtime identity is invalid or differs from its binding",
        )
        _require(
            runtime["environment_sha256"] == manifest.runtime.environment_sha256,
            "capability runtime identity is invalid or differs from its binding",
        )
        _require(
            runtime["environment_source_sha256"] == manifest.runtime.environment_sha256,
            "capability environment must bind its retained source bytes",
        )
        implementation = source(root, runtime["implementation_sha256"])
        _require(
            sha(fresh(runtime["implementation_path"])) == runtime["implementation_sha256"],
            "capability runtime identity is invalid or differs from its binding",
        )
        source(root, runtime["environment_source_sha256"])
        _require(
            sha(fresh(runtime["environment_source_path"])) == runtime["environment_source_sha256"],
            "capability runtime identity is invalid or differs from its binding",
        )
        if manifest.runtime.runtime_kind is RuntimeKind.DETERMINISTIC_FUNCTION:
            module, qualified_name = manifest.runtime.adapter_ref.split(":", 1)
            _require(
                Path(runtime["implementation_path"])
                .as_posix()
                .endswith(module.replace(".", "/") + ".py"),
                "capability runtime identity is invalid or differs from its binding",
            )
            body = ast.parse(implementation).body
            for component in qualified_name.split("."):
                matches = [
                    n
                    for n in body
                    if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == component
                ]
                _require(
                    len(matches) == 1, "declared runtime callable is absent from its actual source"
                )
                body = matches[0].body
        else:
            if manifest.runtime.runtime_kind is not RuntimeKind.DIGEST_PINNED_CONTAINER:
                raise CapabilitySourceVerificationError(
                    "capability source verification failed: manifest.runtime.runtime_kind is RuntimeKind.DIGEST_PINNED_CONTAINER"
                )
            _require(
                runtime["container_workload_path"].startswith("/opt/aletheia/"),
                "capability runtime identity is invalid or differs from its binding",
            )
            _require(
                runtime["environment_source_sha256"] == manifest.runtime.environment_sha256,
                "capability runtime identity is invalid or differs from its binding",
            )
        audit_hashes = set()
        required = set(CapabilityAuditKind)
        if manifest.calibration.mode is CalibrationMode.NOT_APPLICABLE:
            required.remove(CapabilityAuditKind.CALIBRATION)
        _require(
            {b.audit_kind for b in requirement.audit_bindings} == required,
            "capability audit source is invalid or differs from its binding",
        )
        for binding in requirement.audit_bindings:
            record = signed_record(
                root, binding.receipt_sha256, auditor, "aletheia.capability_engineering_audit.v1"
            )
            _require(
                record["definition_sha256"] == subject,
                "capability audit scope or chronology is invalid or differs from its binding",
            )
            _require(
                record["audit_kind"] == binding.audit_kind.value,
                "capability audit scope or chronology is invalid or differs from its binding",
            )
            _require(
                record["audit_policy_sha256"]
                == binding.audit_policy_sha256
                == expected_capability_audit_policy_sha256(manifest, binding.audit_kind),
                "capability audit scope or chronology is invalid or differs from its binding",
            )
            _require(
                record["result"] == "passed",
                "capability audit scope or chronology is invalid or differs from its binding",
            )
            _require(
                binding.auditor_principal_id == auditor["principal_id"],
                "capability auditor independence is invalid or differs from its binding",
            )
            _require(
                binding.valid_from
                <= utc(record["issued_at"])
                <= manifest.qualification.qualified_at,
                "capability qualification is invalid or differs from its binding",
            )
            _require(
                utc(record["expires_at"]) > request.protocol.authored_at,
                "capability audit scope or chronology is invalid or differs from its binding",
            )
            _require(
                utc(record["expires_at"]) <= utc(auditor["expires_at"]),
                "capability auditor independence is invalid or differs from its binding",
            )
            _require(
                binding.expires_at == utc(record["expires_at"]),
                "capability audit scope or chronology is invalid or differs from its binding",
            )
            if runtime["implementation_sha256"] not in record["materials"]:
                raise CapabilitySourceVerificationError(
                    "capability source verification failed: runtime['implementation_sha256'] in record['materials']"
                )
            if runtime["environment_source_sha256"] not in record["materials"]:
                raise CapabilitySourceVerificationError(
                    "capability source verification failed: runtime['environment_source_sha256'] in record['materials']"
                )
            _verify_check_result(root, record)
            audit_hashes.add(binding.receipt_sha256)
        qualified_hashes = set(manifest.qualification.evidence_receipt_sha256s)
        _require(
            audit_hashes <= qualified_hashes and len(qualified_hashes - audit_hashes) == 1,
            "capability audit source is invalid or differs from its binding",
        )
        decision = signed_record(
            root,
            next(iter(qualified_hashes - audit_hashes)),
            qualifier,
            "aletheia.capability_engineering_qualification.v1",
        )
        _require(
            decision["definition_sha256"] == subject,
            "capability qualification decision is invalid or differs from its binding",
        )
        _require(
            decision["audit_receipt_sha256s"] == sorted(audit_hashes),
            "capability qualification decision is invalid or differs from its binding",
        )
        _require(
            set(decision["materials"]) >= audit_hashes,
            "capability qualification decision is invalid or differs from its binding",
        )
        _require(
            decision["result"] == "qualified",
            "capability qualification decision is invalid or differs from its binding",
        )
        _require(
            utc(decision["issued_at"]) == manifest.qualification.qualified_at,
            "capability qualification is invalid or differs from its binding",
        )
        _require(
            utc(decision["expires_at"]) == manifest.qualification.expires_at,
            "capability qualification is invalid or differs from its binding",
        )
        _require(
            manifest.qualification.expires_at <= utc(qualifier["expires_at"]),
            "capability qualifier independence is invalid or differs from its binding",
        )
        _require(
            manifest.qualification.qualified_at
            <= manifest.frozen_at
            <= request.protocol.authored_at,
            "capability qualification is invalid or differs from its binding",
        )
        subjects[manifest.manifest_sha256] = sorted(qualified_hashes)
    return {
        "scientific_authority": False,
        "verified_capabilities": subjects,
        "trust_sha256": canonical_sha256(trust),
        "runtime_sources_sha256": canonical_sha256(runtime_sources),
    }


def schema_sources(root, value):
    """Reopen every schema reference; schema semantics remain the auditor's responsibility."""
    result = set()
    if isinstance(value, dict):
        if value.get("schema_name") == "aletheia.json_schema_ref":
            digest = value["schema_sha256"]
            _require(
                isinstance(json.loads(source(root, digest)), dict), "schema source is not an object"
            )
            result.add(digest)
        for nested in value.values():
            result.update(schema_sources(root, nested))
    elif isinstance(value, (list, tuple)):
        for nested in value:
            result.update(schema_sources(root, nested))
    return result


class CapabilitySourceRuntimeConfigV1(KernelModel):
    """Externally pinned public trust and source inventory for every selected capability."""

    schema_name: Literal["aletheia.capability_source_runtime_config"] = (
        "aletheia.capability_source_runtime_config"
    )
    schema_version: Literal[1] = 1
    source_root: str
    trust_path: str
    trust_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    runtime_sources_path: str
    runtime_sources_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    implementation_source_path: str
    implementation_source_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    scientific_authority: Literal[False] = False

    @model_validator(mode="after")
    def _paths_are_absolute(self):
        for value in (
            self.source_root,
            self.trust_path,
            self.runtime_sources_path,
            self.implementation_source_path,
        ):
            path = Path(value)
            if not path.is_absolute() or ".." in path.parts or str(path) != value:
                raise ValueError("capability source paths must be canonical and absolute")
        if self.trust_path == self.runtime_sources_path:
            raise ValueError("capability trust and runtime inventory must be separate files")
        return self


class PinnedCapabilitySourceVerifier:
    """Freshly reload all source bytes; never issue qualification decisions or cache a pass."""

    def __init__(self, config: CapabilitySourceRuntimeConfigV1):
        self.config = CapabilitySourceRuntimeConfigV1.model_validate(
            config.model_dump(mode="python")
        )

    def verify(self, request, *, observed_at):
        config = self.config
        actual = Path(__file__).resolve(strict=True)
        if actual != Path(config.implementation_source_path):
            raise CapabilitySourceVerificationError("capability verifier module identity differs")
        before = fresh(actual)
        if sha(before) != config.implementation_source_sha256:
            raise CapabilitySourceVerificationError("capability verifier implementation differs")
        data = {}
        for name in ("trust", "runtime_sources"):
            payload = fresh(getattr(config, name + "_path"))
            if sha(payload) != getattr(config, name + "_sha256"):
                raise CapabilitySourceVerificationError(name + " differs from its deployment pin")
            data[name] = json.loads(payload)
            if canonical_json_bytes(data[name]) != payload:
                raise CapabilitySourceVerificationError(name + " is not canonical JSON")
        if utc(observed_at.isoformat()) < request.protocol.authored_at:
            raise CapabilitySourceVerificationError("source verification predates the protocol")
        result = verify_capability_sources(
            request,
            root=config.source_root,
            trust=data["trust"],
            runtime_sources=data["runtime_sources"],
        )
        result["schema_source_sha256s"] = sorted(
            schema_sources(config.source_root, request.model_dump(mode="json"))
        )
        if fresh(actual) != before:
            raise CapabilitySourceVerificationError(
                "capability verifier changed during verification"
            )
        return result
