"""Execute, parse, validate, and replay the F10-S5 ASE/EMT simulation capability."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

from aletheia.capabilities.observations import CapabilityObservationArchive
from aletheia.domains.materials.simulation import (
    AseEmtEosJob,
    AseEmtSimulationBundle,
    AseEmtSimulationProtocol,
    SimulationExecutionStatus,
    SimulationRawRun,
    SimulationSecurityReceipt,
    assemble_ase_emt_simulation_bundle,
    bind_ase_emt_job,
    compare_ase_emt_simulation_reproduction,
    parse_ase_emt_simulation,
    simulation_executor_implementation_sha256,
    validate_ase_emt_simulation,
)
from aletheia.reproducibility.manifest import content_sha256


ModelT = TypeVar("ModelT", bound=BaseModel)
_ALLOWED_OUTPUT_FILES = frozenset({"checkpoint.json", "failure.json", "result.json"})


def _read(path: Path) -> Any:
    resolved = path.expanduser().resolve(strict=True)
    text = resolved.read_text(encoding="utf-8")
    if resolved.suffix.lower() in {".yaml", ".yml"}:
        return yaml.safe_load(text)
    return json.loads(text)


def _model(path: Path, model_type: type[ModelT]) -> ModelT:
    return model_type.model_validate(_read(path))


def _atomic_new_json(path: Path, value: object) -> Path:
    destination = path.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(f"refusing to replace immutable simulation evidence: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if hasattr(value, "model_dump"):
                value = value.model_dump(mode="json", exclude_none=True)  # type: ignore[union-attr]
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
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
    return destination


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _docker(*arguments: str, timeout: float = 30) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ("docker", *arguments),
        check=False,
        capture_output=True,
        timeout=timeout,
    )


def _verify_image(protocol: AseEmtSimulationProtocol) -> None:
    inspected = _docker(
        "image",
        "inspect",
        protocol.container.image_id,
        "--format",
        "{{.Id}} {{.Os}}/{{.Architecture}}",
    )
    expected = f"{protocol.container.image_id} {protocol.container.platform}\n".encode()
    if inspected.returncode != 0 or inspected.stdout != expected:
        raise RuntimeError("frozen simulation image ID/platform is not locally available")


def _container_absent(name: str) -> None:
    inspected = _docker("inspect", name)
    if inspected.returncode == 0:
        raise RuntimeError(f"refusing to reuse an existing simulation container: {name}")


def _container_state(name: str) -> tuple[dict[str, Any] | None, bytes]:
    inspected = _docker("inspect", name, "--format", "{{json .State}}")
    if inspected.returncode != 0:
        return None, inspected.stderr or inspected.stdout
    return json.loads(inspected.stdout), inspected.stdout


def _cleanup_container(name: str, *, force: bool) -> bytes:
    if force:
        _docker("stop", "--timeout", "1", name, timeout=10)
    arguments = ("rm", "--force", name) if force else ("rm", name)
    removed = _docker(*arguments, timeout=10)
    return removed.stdout + removed.stderr


def _read_outputs(directory: Path, *, file_limit: int, bytes_limit: int) -> dict[str, bytes]:
    entries = tuple(sorted(directory.iterdir(), key=lambda item: item.name))
    if len(entries) > file_limit:
        raise OverflowError("simulation output file-count quota exceeded")
    if any(
        item.name not in _ALLOWED_OUTPUT_FILES
        or item.is_symlink()
        or not stat.S_ISREG(item.stat(follow_symlinks=False).st_mode)
        for item in entries
    ):
        raise ValueError("simulation output contains an unsafe or unknown file")
    total = sum(item.stat().st_size for item in entries)
    if total > bytes_limit:
        raise OverflowError("simulation output byte quota exceeded")
    return {item.name: item.read_bytes() for item in entries}


def _artifact_media_type(name: str) -> str:
    return "application/json" if name.endswith("json") else "text/plain; charset=utf-8"


def _execute(args: argparse.Namespace) -> None:
    protocol = _model(args.protocol, AseEmtSimulationProtocol)
    job_path = args.job.expanduser().resolve(strict=True)
    exact_job_bytes = job_path.read_bytes()
    job = AseEmtEosJob.model_validate_json(exact_job_bytes)
    bind_ase_emt_job(protocol=protocol, job=job, exact_job_bytes=exact_job_bytes)
    _verify_image(protocol)
    if simulation_executor_implementation_sha256() != protocol.host_executor_implementation_sha256:
        raise ValueError("host simulation executor differs from frozen protocol")

    container_name = f"aletheia-sim-{hashlib.sha256(args.run_id.encode()).hexdigest()[:24]}"
    _container_absent(container_name)
    scratch_parent = args.archive.expanduser().resolve(strict=False).parent
    scratch_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_directory = Path(
        tempfile.mkdtemp(prefix=".aletheia-ase-emt-output-", dir=scratch_parent)
    )
    os.chmod(output_directory, 0o777)
    container = protocol.container
    command = (
        "docker",
        "run",
        "--name",
        container_name,
        "--init",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(container.pids_limit),
        "--memory",
        f"{container.memory_mebibytes}m",
        "--cpus",
        str(container.cpus),
        "--user",
        container.user,
        "--mount",
        f"type=bind,src={job_path},dst=/input/job.json,readonly",
        "--mount",
        f"type=bind,src={output_directory},dst=/output",
        container.image_id,
        "/input/job.json",
        "/output",
    )
    started_at = datetime.now(timezone.utc)
    process: subprocess.CompletedProcess[bytes] | None = None
    timed_out = False
    stdout = b""
    stderr = b""
    try:
        try:
            process = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=container.timeout_seconds,
            )
            stdout = process.stdout
            stderr = process.stderr
        except subprocess.TimeoutExpired as error:
            timed_out = True
            stdout = error.stdout or b""
            stderr = error.stderr or b""
        state, state_bytes = _container_state(container_name)
        if timed_out or state is None or state.get("Status") != "exited":
            cleanup_bytes = _cleanup_container(container_name, force=True)
        else:
            cleanup_bytes = _cleanup_container(container_name, force=False)
        ended_at = datetime.now(timezone.utc)

        try:
            output_payloads = _read_outputs(
                output_directory,
                file_limit=container.output_file_limit,
                bytes_limit=container.output_bytes_limit,
            )
            quota_error = None
        except OverflowError as error:
            output_payloads = {}
            quota_error = str(error)

        if timed_out:
            status = SimulationExecutionStatus.TIMED_OUT
            failure_kind = "wall_clock_timeout"
            exit_code = None
        elif quota_error is not None:
            status = SimulationExecutionStatus.OUTPUT_QUOTA_EXCEEDED
            failure_kind = "output_quota_exceeded"
            exit_code = process.returncode if process else None
        elif (
            process is not None
            and process.returncode == 0
            and state is not None
            and state.get("Status") == "exited"
            and state.get("ExitCode") == 0
            and {"checkpoint.json", "result.json"}.issubset(output_payloads)
        ):
            status = SimulationExecutionStatus.SUCCEEDED
            failure_kind = None
            exit_code = 0
        elif process is not None and process.returncode != 0:
            status = SimulationExecutionStatus.FAILED
            failure_kind = "worker_nonzero_exit"
            exit_code = process.returncode
        else:
            status = SimulationExecutionStatus.INFRASTRUCTURE_FAILURE
            failure_kind = "container_state_inconsistent"
            exit_code = process.returncode if process else None

        archive = CapabilityObservationArchive(args.archive)
        payloads: dict[str, tuple[bytes, str]] = {
            "input-job-json": (exact_job_bytes, "application/json"),
            "docker-state-json": (state_bytes or b"{}\n", "application/json"),
            "cleanup-log": (cleanup_bytes or b"cleanup completed\n", "text/plain; charset=utf-8"),
        }
        if stdout:
            payloads["stdout"] = (stdout, "text/plain; charset=utf-8")
        if stderr:
            payloads["stderr"] = (stderr, "text/plain; charset=utf-8")
        if quota_error is not None:
            payloads["quota-error"] = (quota_error.encode(), "text/plain; charset=utf-8")
        for name, payload in output_payloads.items():
            payloads[name.replace(".", "-")] = (payload, _artifact_media_type(name))
        artifacts = tuple(
            sorted(
                (
                    archive.store(
                        artifact_id=artifact_id,
                        payload=payload,
                        media_type=media_type,
                        captured_at=ended_at,
                    )
                    for artifact_id, (payload, media_type) in payloads.items()
                ),
                key=lambda item: item.artifact_id,
            )
        )
        raw_run = SimulationRawRun(
            run_id=args.run_id,
            protocol_sha256=protocol.protocol_sha256,
            job_sha256=job.job_sha256,
            exact_job_bytes_sha256=hashlib.sha256(exact_job_bytes).hexdigest(),
            image_id=container.image_id,
            worker_sha256=container.worker_sha256,
            executor_implementation_sha256=simulation_executor_implementation_sha256(),
            command_sha256=content_sha256(list(command)),
            security=SimulationSecurityReceipt(
                pids_limit=container.pids_limit,
                memory_mebibytes=container.memory_mebibytes,
                cpus=container.cpus,
                timeout_seconds=container.timeout_seconds,
            ),
            status=status,
            exit_code=exit_code,
            failure_kind=failure_kind,
            artifacts=artifacts,
            started_at=started_at,
            ended_at=ended_at,
            checkpoint_retained="checkpoint.json" in output_payloads,
        )
        destination = _atomic_new_json(args.output, raw_run)
        _print(
            {
                "raw_run": str(destination),
                "raw_run_sha256": raw_run.run_sha256,
                "status": raw_run.status.value,
                "exit_code": raw_run.exit_code,
                "checkpoint_retained": raw_run.checkpoint_retained,
                "artifact_ids": [item.artifact_id for item in raw_run.artifacts],
                "image_id": raw_run.image_id,
                "security": raw_run.security.model_dump(mode="json"),
            }
        )
    finally:
        shutil.rmtree(output_directory, ignore_errors=True)


def _finalize(args: argparse.Namespace) -> None:
    protocol = _model(args.protocol, AseEmtSimulationProtocol)
    job_path = args.job.expanduser().resolve(strict=True)
    exact_job_bytes = job_path.read_bytes()
    job = AseEmtEosJob.model_validate_json(exact_job_bytes)
    raw_run = _model(args.raw_run, SimulationRawRun)
    archive = CapabilityObservationArchive(args.archive)
    artifacts = {item.artifact_id: archive.read(item) for item in raw_run.artifacts}
    parsed_at = datetime.now(timezone.utc)
    parse_result = parse_ase_emt_simulation(
        protocol=protocol,
        job=job,
        exact_job_bytes=exact_job_bytes,
        raw_run=raw_run,
        artifacts=artifacts,
        parsed_at=parsed_at,
    )
    validation = validate_ase_emt_simulation(
        protocol=protocol,
        job=job,
        raw_run=raw_run,
        parse_result=parse_result,
        validated_at=datetime.now(timezone.utc),
    )
    bundle = assemble_ase_emt_simulation_bundle(
        protocol=protocol,
        job=job,
        raw_run=raw_run,
        parse_result=parse_result,
        validation=validation,
    )
    destination = _atomic_new_json(args.output, bundle)
    result = parse_result.candidate.result if parse_result.candidate else None
    _print(
        {
            "bundle": str(destination),
            "bundle_sha256": bundle.bundle_sha256,
            "raw_run_sha256": raw_run.run_sha256,
            "parse_sha256": parse_result.parse_sha256,
            "validation_sha256": validation.validation_sha256,
            "disposition": validation.disposition.value,
            "all_required_checks_passed": validation.all_required_checks_passed,
            "checks": [item.model_dump(mode="json") for item in validation.checks],
            "result_payload_sha256": result.payload_sha256 if result else None,
            "equilibrium_volume_per_atom_angstrom3": (
                result.equilibrium_volume_per_atom_angstrom3 if result else None
            ),
            "bulk_modulus_eV_per_angstrom3": (
                result.bulk_modulus_eV_per_angstrom3 if result else None
            ),
            "claim_ceiling": validation.claim_ceiling,
        }
    )


def _verify(args: argparse.Namespace) -> None:
    bundle = _model(args.bundle, AseEmtSimulationBundle)
    job_path = args.job.expanduser().resolve(strict=True)
    exact_job_bytes = job_path.read_bytes()
    archive = CapabilityObservationArchive(args.archive)
    artifacts = {item.artifact_id: archive.read(item) for item in bundle.raw_run.artifacts}
    replay_parse = parse_ase_emt_simulation(
        protocol=bundle.protocol,
        job=bundle.job,
        exact_job_bytes=exact_job_bytes,
        raw_run=bundle.raw_run,
        artifacts=artifacts,
        parsed_at=(
            bundle.parse_result.candidate.parsed_at
            if bundle.parse_result.candidate
            else bundle.parse_result.failure.failed_at  # type: ignore[union-attr]
        ),
    )
    replay_validation = validate_ase_emt_simulation(
        protocol=bundle.protocol,
        job=bundle.job,
        raw_run=bundle.raw_run,
        parse_result=replay_parse,
        validated_at=bundle.validation.validated_at,
    )
    if replay_parse != bundle.parse_result or replay_validation != bundle.validation:
        raise ValueError("simulation bundle differs from exact raw-artifact replay")
    _print(
        {
            "bundle_sha256": bundle.bundle_sha256,
            "all_raw_artifacts_rehashed": True,
            "checkpoint_reparsed": True,
            "result_reparsed": True,
            "quality_and_gold_checks_recomputed": True,
            "bundle_exactly_replayed": True,
            "disposition": bundle.validation.disposition.value,
        }
    )


def _compare(args: argparse.Namespace) -> None:
    source = _model(args.source_bundle, AseEmtSimulationBundle)
    replay = _model(args.replay_bundle, AseEmtSimulationBundle)
    receipt = compare_ase_emt_simulation_reproduction(
        source=source,
        replay=replay,
        compared_at=datetime.now(timezone.utc),
    )
    destination = _atomic_new_json(args.output, receipt)
    _print(
        {
            "receipt": str(destination),
            "receipt_sha256": receipt.receipt_sha256,
            "source_bundle_sha256": receipt.source_bundle_sha256,
            "replay_bundle_sha256": receipt.replay_bundle_sha256,
            "exact_result_payload_match": receipt.exact_result_payload_match,
            "result_payload_sha256": receipt.source_result_payload_sha256,
            "same_image_and_implementation_not_independent_replication": (
                receipt.same_image_and_implementation_not_independent_replication
            ),
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    execute = subparsers.add_parser("execute", help="run one immutable hardened container attempt")
    execute.add_argument("--protocol", type=Path, required=True)
    execute.add_argument("--job", type=Path, required=True)
    execute.add_argument("--run-id", required=True)
    execute.add_argument("--archive", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.set_defaults(handler=_execute)

    finalize = subparsers.add_parser(
        "finalize", help="reopen raw bytes, parse, validate, and create a bounded bundle"
    )
    finalize.add_argument("--protocol", type=Path, required=True)
    finalize.add_argument("--job", type=Path, required=True)
    finalize.add_argument("--raw-run", type=Path, required=True)
    finalize.add_argument("--archive", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    finalize.set_defaults(handler=_finalize)

    verify = subparsers.add_parser("verify", help="rehash and exactly replay a retained bundle")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--job", type=Path, required=True)
    verify.add_argument("--archive", type=Path, required=True)
    verify.set_defaults(handler=_verify)

    compare = subparsers.add_parser(
        "compare", help="compare two distinct validated physical runs under one frozen image"
    )
    compare.add_argument("--source-bundle", type=Path, required=True)
    compare.add_argument("--replay-bundle", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.set_defaults(handler=_compare)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
