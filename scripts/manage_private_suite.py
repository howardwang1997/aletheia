"""Operate evaluator-owned private-suite custody without accepting raw keys on the CLI.

The materialization operator factory uses ``MODULE:CALLABLE`` and receives only frozen manifests
and an operator configuration object.  It must return ``{"store": ..., "decryptor": ...}`` (or a
two-item tuple).  The store and decryptor resolve ciphertext and KMS credentials outside Aletheia's
custody ledger; neither plaintext nor key bytes are serialized by this command.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from aletheia.evals.baselines import BaselineMatrixPlan
from aletheia.evals.private_suite import (
    PrivateCiphertextStore,
    PrivateContaminationReport,
    PrivateCustodyLedger,
    PrivateEnvelopeDecryptor,
    PrivateRetirementRecord,
    PrivateSuiteAccessAuthorization,
    PrivateSuiteManifest,
    close_private_suite_access,
    fail_private_suite_materialization,
    load_materialized_private_suite,
    materialize_private_suite,
)


def _read_json(path: Path) -> Any:
    resolved = path.expanduser().resolve(strict=True)
    return json.loads(resolved.read_text(encoding="utf-8"))


def _load_model(path: Path, model: Any, *, wrapper: str | None = None) -> Any:
    raw = _read_json(path)
    if wrapper is not None and isinstance(raw, dict) and wrapper in raw:
        raw = raw[wrapper]
    return model.model_validate(raw)


def _atomic_new_json(path: Path, payload: object) -> None:
    destination = path.expanduser().resolve(strict=False)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"refusing to replace frozen output: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if hasattr(payload, "model_dump"):
                payload = payload.model_dump(mode="json", exclude_none=True)  # type: ignore[union-attr]
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _print_json(payload: object) -> None:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json", exclude_none=True)  # type: ignore[union-attr]
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _manifest(path: Path) -> PrivateSuiteManifest:
    return _load_model(path, PrivateSuiteManifest, wrapper="manifest")


def _ledger(path: Path) -> PrivateCustodyLedger:
    return PrivateCustodyLedger(path)


def _status_payload(manifest: PrivateSuiteManifest, ledger: PrivateCustodyLedger) -> dict[str, Any]:
    return {
        "schema_name": "aletheia.private_suite_status",
        "schema_version": 1,
        "suite_id": manifest.suite_id,
        "version": manifest.version,
        "manifest_sha256": manifest.manifest_sha256,
        "state": ledger.state(manifest).model_dump(mode="json", exclude_none=True),
        "ledger": ledger.assert_integrity(),
    }


def _operator_factory(reference: str) -> Callable[..., Any]:
    module_name, separator, attribute_name = reference.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("operator factories must use MODULE:CALLABLE")
    value = getattr(importlib.import_module(module_name), attribute_name)
    if not callable(value):
        raise TypeError(f"operator factory {reference!r} is not callable")
    return value


def _operators(raw: Any) -> tuple[PrivateCiphertextStore, PrivateEnvelopeDecryptor]:
    if isinstance(raw, Mapping):
        store, decryptor = raw.get("store"), raw.get("decryptor")
    elif isinstance(raw, (tuple, list)) and len(raw) == 2:
        store, decryptor = raw
    else:
        raise TypeError("operator factory must return a store/decryptor mapping or pair")
    if not callable(getattr(store, "read_ciphertext", None)):
        raise TypeError("private ciphertext store lacks read_ciphertext(storage_ref)")
    if not callable(getattr(decryptor, "decrypt", None)):
        raise TypeError("private envelope decryptor lacks decrypt(envelope, ciphertext)")
    return store, decryptor


def _validate(args: argparse.Namespace) -> None:
    manifest = _manifest(args.manifest)
    _print_json(
        {
            "schema_name": "aletheia.private_suite_validation",
            "schema_version": 1,
            "suite_id": manifest.suite_id,
            "version": manifest.version,
            "tier": manifest.tier.value,
            "manifest_sha256": manifest.manifest_sha256,
            "task_count": len(manifest.tasks),
            "domains": sorted({task.domain for task in manifest.tasks}),
            "case_types": sorted({task.case_type.value for task in manifest.tasks}),
            "encrypted_asset_count": 1 + 3 * len(manifest.tasks),
        }
    )


def _register(args: argparse.Namespace) -> None:
    manifest = _manifest(args.manifest)
    ledger = _ledger(args.ledger)
    ledger.register_suite(manifest)
    _print_json(_status_payload(manifest, ledger))


def _authorize(args: argparse.Namespace) -> None:
    manifest = _manifest(args.manifest)
    authorization = _load_model(
        args.authorization, PrivateSuiteAccessAuthorization, wrapper="authorization"
    )
    ledger = _ledger(args.ledger)
    ledger.authorize_access(manifest, authorization)
    _print_json(_status_payload(manifest, ledger))


def _materialize(args: argparse.Namespace) -> None:
    output = args.output.expanduser().resolve(strict=False)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"refusing to spend private access for existing output: {output}")
    manifest = _manifest(args.manifest)
    authorization = _load_model(
        args.authorization, PrivateSuiteAccessAuthorization, wrapper="authorization"
    )
    matrix = _load_model(args.matrix, BaselineMatrixPlan, wrapper="matrix")
    ledger = _ledger(args.ledger)
    # Resolve and validate custody state before loading an operator-controlled KMS plugin.
    ledger.state(manifest)
    registered_authorization = ledger.authorization(manifest, authorization.authorization_id)
    if registered_authorization != authorization:
        raise ValueError("authorization file differs from the custody registry")
    config = _read_json(args.operator_config) if args.operator_config else {}
    factory = _operator_factory(args.operator_factory)
    store, decryptor = _operators(
        factory(
            manifest=manifest,
            authorization=authorization,
            matrix=matrix,
            config=config,
        )
    )
    materialized = materialize_private_suite(
        manifest=manifest,
        authorization=authorization,
        baseline_matrix=matrix,
        ledger=ledger,
        store=store,
        decryptor=decryptor,
        evaluator_root=args.evaluator_root,
    )
    _atomic_new_json(
        output,
        {
            "schema_name": "aletheia.private_suite_materialization_receipt",
            "schema_version": 1,
            "receipt": materialized.receipt.model_dump(mode="json", exclude_none=True),
        },
    )
    print(output.resolve(strict=True))


def _report_contamination(args: argparse.Namespace) -> None:
    manifest = _manifest(args.manifest)
    report = _load_model(args.report, PrivateContaminationReport, wrapper="report")
    ledger = _ledger(args.ledger)
    before = ledger.state(manifest)
    if (
        before.materialization_receipt_sha256 is not None
        and not before.access_closed
        and args.evaluator_root is None
    ):
        raise ValueError("active materialized contamination requires --evaluator-root cleanup")
    ledger.report_contamination(manifest, report)
    if before.materialization_receipt_sha256 is not None and not before.access_closed:
        assert before.opened_access_id is not None
        close_private_suite_access(
            manifest=manifest,
            ledger=ledger,
            access_id=before.opened_access_id,
            evaluator_root=args.evaluator_root,
            closed_at=report.detected_at,
        )
    _print_json(_status_payload(manifest, ledger))


def _recover_materialized(args: argparse.Namespace) -> None:
    output = args.output.expanduser().resolve(strict=False)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"refusing to replace recovered receipt: {output}")
    manifest = _manifest(args.manifest)
    ledger = _ledger(args.ledger)
    materialized = load_materialized_private_suite(
        manifest=manifest,
        ledger=ledger,
        access_id=args.access_id,
        evaluator_root=args.evaluator_root,
    )
    _atomic_new_json(
        output,
        {
            "schema_name": "aletheia.private_suite_materialization_receipt",
            "schema_version": 1,
            "receipt": materialized.receipt.model_dump(mode="json", exclude_none=True),
        },
    )
    print(output.resolve(strict=True))


def _close(args: argparse.Namespace) -> None:
    manifest = _manifest(args.manifest)
    ledger = _ledger(args.ledger)
    receipt = close_private_suite_access(
        manifest=manifest,
        ledger=ledger,
        access_id=args.access_id,
        evaluator_root=args.evaluator_root,
    )
    _print_json(
        {
            "cleanup_receipt": receipt.model_dump(mode="json", exclude_none=True),
            "status": _status_payload(manifest, ledger),
        }
    )


def _fail_materialization(args: argparse.Namespace) -> None:
    manifest = _manifest(args.manifest)
    ledger = _ledger(args.ledger)
    receipt = fail_private_suite_materialization(
        manifest=manifest,
        ledger=ledger,
        access_id=args.access_id,
        evaluator_root=args.evaluator_root,
        error_evidence_sha256=args.error_evidence_sha256,
    )
    _print_json(
        {
            "cleanup_receipt": receipt.model_dump(mode="json", exclude_none=True),
            "status": _status_payload(manifest, ledger),
        }
    )


def _retire(args: argparse.Namespace) -> None:
    manifest = _manifest(args.manifest)
    retirement = _load_model(args.retirement, PrivateRetirementRecord, wrapper="retirement")
    ledger = _ledger(args.ledger)
    before = ledger.state(manifest)
    if (
        before.materialization_receipt_sha256 is not None
        and not before.access_closed
        and args.evaluator_root is None
    ):
        raise ValueError("active materialized retirement requires --evaluator-root cleanup")
    ledger.retire(manifest, retirement)
    if before.materialization_receipt_sha256 is not None and not before.access_closed:
        assert before.opened_access_id is not None
        close_private_suite_access(
            manifest=manifest,
            ledger=ledger,
            access_id=before.opened_access_id,
            evaluator_root=args.evaluator_root,
            closed_at=retirement.retired_at,
        )
    _print_json(_status_payload(manifest, ledger))


def _status(args: argparse.Namespace) -> None:
    manifest = _manifest(args.manifest)
    payload = _status_payload(manifest, _ledger(args.ledger))
    if args.output is not None:
        _atomic_new_json(args.output, payload)
        print(args.output.expanduser().resolve(strict=True))
    else:
        _print_json(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage one-time evaluator custody for prospective private test suites."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate custody schema without opening it")
    validate.add_argument("--manifest", type=Path, required=True)
    validate.set_defaults(handler=_validate)

    register = subparsers.add_parser("register", help="freeze a suite in the custody ledger")
    register.add_argument("--manifest", type=Path, required=True)
    register.add_argument("--ledger", type=Path, required=True)
    register.set_defaults(handler=_register)

    authorize = subparsers.add_parser(
        "authorize", help="record a frozen two-person access authorization"
    )
    authorize.add_argument("--manifest", type=Path, required=True)
    authorize.add_argument("--authorization", type=Path, required=True)
    authorize.add_argument("--ledger", type=Path, required=True)
    authorize.set_defaults(handler=_authorize)

    materialize = subparsers.add_parser(
        "materialize", help="consume the one-time unlock through an external KMS operator"
    )
    materialize.add_argument("--manifest", type=Path, required=True)
    materialize.add_argument("--authorization", type=Path, required=True)
    materialize.add_argument("--matrix", type=Path, required=True)
    materialize.add_argument("--ledger", type=Path, required=True)
    materialize.add_argument("--operator-factory", required=True, metavar="MODULE:CALLABLE")
    materialize.add_argument("--operator-config", type=Path)
    materialize.add_argument("--evaluator-root", type=Path, required=True)
    materialize.add_argument("--output", type=Path, required=True)
    materialize.set_defaults(handler=_materialize)

    contamination = subparsers.add_parser(
        "report-contamination", help="retire contaminated material and clean active plaintext"
    )
    contamination.add_argument("--manifest", type=Path, required=True)
    contamination.add_argument("--report", type=Path, required=True)
    contamination.add_argument("--ledger", type=Path, required=True)
    contamination.add_argument("--evaluator-root", type=Path)
    contamination.set_defaults(handler=_report_contamination)

    recover = subparsers.add_parser(
        "recover-materialized",
        help="verify staged plaintext and recover a lost committed receipt",
    )
    recover.add_argument("--manifest", type=Path, required=True)
    recover.add_argument("--ledger", type=Path, required=True)
    recover.add_argument("--access-id", required=True)
    recover.add_argument("--evaluator-root", type=Path, required=True)
    recover.add_argument("--output", type=Path, required=True)
    recover.set_defaults(handler=_recover_materialized)

    close = subparsers.add_parser(
        "close", help="verify and dispose plaintext, then close the one-time access"
    )
    close.add_argument("--manifest", type=Path, required=True)
    close.add_argument("--ledger", type=Path, required=True)
    close.add_argument("--access-id", required=True)
    close.add_argument("--evaluator-root", type=Path, required=True)
    close.set_defaults(handler=_close)

    failed = subparsers.add_parser(
        "fail-materialization",
        help="retire and clean an access left open by a crashed materializer",
    )
    failed.add_argument("--manifest", type=Path, required=True)
    failed.add_argument("--ledger", type=Path, required=True)
    failed.add_argument("--access-id", required=True)
    failed.add_argument("--evaluator-root", type=Path, required=True)
    failed.add_argument(
        "--error-evidence-sha256",
        required=True,
        help="SHA-256 of the external crash/incident evidence",
    )
    failed.set_defaults(handler=_fail_materialization)

    retire = subparsers.add_parser("retire", help="record a two-person task or suite retirement")
    retire.add_argument("--manifest", type=Path, required=True)
    retire.add_argument("--retirement", type=Path, required=True)
    retire.add_argument("--ledger", type=Path, required=True)
    retire.add_argument("--evaluator-root", type=Path)
    retire.set_defaults(handler=_retire)

    status = subparsers.add_parser("status", help="verify the hash chain and show custody state")
    status.add_argument("--manifest", type=Path, required=True)
    status.add_argument("--ledger", type=Path, required=True)
    status.add_argument("--output", type=Path)
    status.set_defaults(handler=_status)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
