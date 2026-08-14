"""Frozen, license-audited Asta CORE-Bench-Hard reproduction adapter.

The adapter intentionally uses only the public CORE-Bench train split (AstaBench's validation
split) for engineering and development.  It never downloads or decrypts the public benchmark's
test archive.  A candidate reproduction program sees one sanitized capsule and writes
``report.json`` plus tangible reproduction output; hidden answers and scorer code exist only in a
separate evaluator container.
"""

from __future__ import annotations

import hashlib
import gzip
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, Sequence

from pydantic import Field, model_validator

from aletheia.coder.executor import (
    docker_execution_is_infrastructure_failure,
    hardened_docker_command,
    run_hardened_container,
)
from aletheia.evals.runner import EvaluationScorerInfrastructureError
from aletheia.evals.schemas import (
    ArtifactRequirement,
    ContaminationPolicy,
    EvalLayer,
    EvaluationPublicAsset,
    EvaluationScore,
    EvaluationSubmission,
    EvaluationSuite,
    EvaluationTask,
    ExecutionExitReason,
    FrozenModel,
    InvalidReason,
    ResourceBudget,
    content_sha256,
)

ASTABENCH_REPOSITORY_URL = "https://github.com/allenai/asta-bench"
ASTABENCH_VERSION = "v0.3.1"
ASTABENCH_COMMIT = "5c844b7451e3a98cd0df71ea626bb217803d2bed"
ASTABENCH_CORE_WRAPPER_SHA256 = (
    "4aed0cd36bd6c48bc352bc32bd5b720e6dccbae0438b386e71218015bc4229b3"
)
INSPECT_EVALS_COMMIT = "c2bec9ebee7a5995512bf5ff67a2e82afe4d12e1"
INSPECT_EVALS_SCORER_SHA256 = (
    "51652922721d62e9f333106f0c015fcd979b20e5cec92895dc364545ff770da3"
)
INSPECT_EVALS_UTILS_SHA256 = (
    "e7ea414a0397a51722e57d5485ba99ac9dc53d19c64ba3b8f69ee0726d8cc24f"
)
COREBENCH_REPOSITORY_URL = "https://github.com/siegelz/core-bench"
COREBENCH_DATASET_ID = "siegelz/core-bench"
COREBENCH_DATASET_REVISION = "18ac8edf2532d9edb9d13ae71f715410de6ee5a0"
COREBENCH_TRAIN_JSON_SHA256 = (
    "3df47f1b3fa1cb60045018eb1a0f1ad4ecf6a53f72318c845a879ce0313b0730"
)
COREBENCH_CAPSULE_URL = "https://corebench.cs.princeton.edu/capsules/{capsule_id}.tar.gz"
COREBENCH_CANARY = "4d66db91-c23a-4c87-9512-aa9b3234ea64"
DEFAULT_COREBENCH_CAPSULE_IDS = ("capsule-6460826", "capsule-0940461")
OFFICIAL_CAPSULE_ARCHIVE_SHA256S = {
    "capsule-6460826": "36e6fe89a288dc66a167c055bd21965cdbd053f87217d6fc9e00642af0445664",
    "capsule-0940461": "4d1ca989f9c597a7ec9f5f0545f7c26943c324426d2f98ff8e54787a696cab31",
}
OFFICIAL_CAPSULE_ARCHIVE_BYTES = {
    "capsule-6460826": 13_752,
    "capsule-0940461": 107_572,
}
OFFICIAL_CAPSULE_CODE_LICENSE_SHA256S = {
    "capsule-6460826": "86f24132a2af026b022666ac11bef2c3b73575ec072f4d33f750e6a9255a486f",
    "capsule-0940461": "4922c02733338b77baf1be5d89b7c066d14bb5a81de992697715b5ca7d3ab35e",
}
OFFICIAL_CAPSULE_DATA_LICENSE_SHA256S = {
    "capsule-6460826": "36ffd9dc085d529a7e60e1276d73ae5a030b020313e6c5408593a6ae2af39673",
    "capsule-0940461": "36ffd9dc085d529a7e60e1276d73ae5a030b020313e6c5408593a6ae2af39673",
}
OFFICIAL_CAPSULE_ENVIRONMENT_SHA256S = {
    "capsule-6460826": "702167a43f0048c7965c50fb53bd1402a63144a9980fd21ed7c7077b5376fb7e",
    "capsule-0940461": "0c9a32ea7da350a7dfc6517146056b4beea9155584e07ec82ff18285398b6297",
}
OFFICIAL_CAPSULE_REQUIREMENTS = {
    "capsule-6460826": ("numpy", "pandas", "scikit-learn", "networkx"),
    "capsule-0940461": (
        "numpy",
        "pandas",
        "scikit-learn",
        "seaborn",
        "jupyter",
        "nbconvert",
    ),
}

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CAPSULE_ID = re.compile(r"^capsule-[0-9]{7}$")
_FORBIDDEN_PROGRAM_REFERENCES = (
    re.compile(r"(?<![A-Za-z0-9_])results(?:[/\\]|$)", re.IGNORECASE),
    re.compile(r"REPRODUCING\.md", re.IGNORECASE),
    re.compile(r"COREBENCH_GOLD", re.IGNORECASE),
    re.compile(r"core_(?:train|test)\.json", re.IGNORECASE),
    re.compile(r"eval(?:uator)?_answers?", re.IGNORECASE),
    re.compile(r"scorer_entrypoint", re.IGNORECASE),
)


def _canonical_bytes(value: FrozenModel | dict[str, Any] | list[Any]) -> bytes:
    if isinstance(value, FrozenModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path, *, max_bytes: int | None = None) -> str:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"asset must be a regular non-symlink file: {path}")
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise ValueError(f"asset exceeds its byte limit: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _tree_manifest(root: Path) -> tuple[str, int, int]:
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"asset tree is not a directory: {root}")
    digest = hashlib.sha256(b"aletheia-corebench-tree-v1\0")
    files = 0
    total_bytes = 0
    for candidate in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if candidate.is_symlink():
            raise ValueError(f"symlink is forbidden in CORE-Bench assets: {candidate}")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ValueError(f"non-regular CORE-Bench asset is forbidden: {candidate}")
        relative = candidate.relative_to(root).as_posix().encode("utf-8")
        size = candidate.stat().st_size
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(size.to_bytes(8, "big"))
        with candidate.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
        files += 1
        total_bytes += size
    return digest.hexdigest(), files, total_bytes


def _archive_inventory(path: Path, *, expected_root: str) -> tuple[int, int]:
    files = 0
    total_bytes = 0
    seen: set[PurePosixPath] = set()
    try:
        archive = tarfile.open(path, mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise ValueError(f"CORE-Bench capsule is not a valid tar.gz: {exc}") from exc
    try:
        for member in archive.getmembers():
            if "\\" in member.name:
                raise ValueError("CORE-Bench capsule uses a non-portable archive path")
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or not relative.parts or any(
                part in {"", ".", ".."} for part in relative.parts
            ):
                raise ValueError("CORE-Bench capsule contains an unsafe archive path")
            if relative.parts[0] != expected_root:
                raise ValueError("CORE-Bench capsule has an unexpected archive root")
            if relative in seen:
                raise ValueError("CORE-Bench capsule contains duplicate archive paths")
            seen.add(relative)
            if not (member.isdir() or member.isreg()):
                raise ValueError("CORE-Bench capsule may contain only directories and regular files")
            if member.isreg():
                files += 1
                total_bytes += member.size
    finally:
        archive.close()
    if not files:
        raise ValueError("CORE-Bench capsule contains no files")
    return files, total_bytes


def _read_tar_member(path: Path, member_name: str) -> bytes:
    with tarfile.open(path, mode="r:gz") as archive:
        try:
            member = archive.getmember(member_name)
        except KeyError as exc:
            raise ValueError(f"CORE-Bench capsule omits {member_name}") from exc
        if not member.isreg():
            raise ValueError(f"CORE-Bench member is not a regular file: {member_name}")
        handle = archive.extractfile(member)
        if handle is None:
            raise ValueError(f"CORE-Bench member cannot be read: {member_name}")
        return handle.read()


def _write_deterministic_tar(root: Path, target: Path) -> tuple[str, int, int, int]:
    """Create a stable regular-file archive; source licenses remain inside the capsule."""

    paths = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    file_count = 0
    expanded_bytes = 0
    with target.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for path in paths:
                    if path.is_symlink():
                        raise ValueError("sanitized CORE-Bench capsule cannot contain symlinks")
                    relative = path.relative_to(root).as_posix()
                    info = tarfile.TarInfo(relative + ("/" if path.is_dir() else ""))
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    if path.is_dir():
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o755
                        archive.addfile(info)
                        continue
                    if not path.is_file():
                        raise ValueError("sanitized CORE-Bench capsule contains a non-regular asset")
                    payload = path.read_bytes()
                    info.size = len(payload)
                    info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                    archive.addfile(info, io.BytesIO(payload))
                    file_count += 1
                    expanded_bytes += len(payload)
    return _sha256_file(target), target.stat().st_size, file_count, expanded_bytes


def _safe_extract_capsule(source: Path, destination: Path, *, expected_root: str) -> None:
    """Extract reviewed regular files while dropping the single upstream root component."""

    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    with tarfile.open(source, mode="r:gz") as archive:
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if relative.parts == (expected_root,):
                continue
            stripped = PurePosixPath(*relative.parts[1:])
            if not stripped.parts:
                continue
            target = destination.joinpath(*stripped.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            if member.isdir():
                target.mkdir(exist_ok=True, mode=0o755)
                continue
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError("CORE-Bench archive member cannot be read")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(target, flags, 0o755 if member.mode & 0o111 else 0o644)
            with handle, os.fdopen(descriptor, "wb") as output:
                shutil.copyfileobj(handle, output, length=1024 * 1024)


class CoreBenchCapsuleContract(FrozenModel):
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_bytes: int = Field(gt=0)
    code_license_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_license_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_dockerfile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_distributions: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _requirements_are_unique(self) -> "CoreBenchCapsuleContract":
        if len(self.required_distributions) != len(set(self.required_distributions)):
            raise ValueError("CORE-Bench capsule requirements must be unique")
        return self


def _official_capsule_contracts() -> dict[str, CoreBenchCapsuleContract]:
    return {
        capsule_id: CoreBenchCapsuleContract(
            archive_sha256=OFFICIAL_CAPSULE_ARCHIVE_SHA256S[capsule_id],
            archive_bytes=OFFICIAL_CAPSULE_ARCHIVE_BYTES[capsule_id],
            code_license_sha256=OFFICIAL_CAPSULE_CODE_LICENSE_SHA256S[capsule_id],
            data_license_sha256=OFFICIAL_CAPSULE_DATA_LICENSE_SHA256S[capsule_id],
            environment_dockerfile_sha256=OFFICIAL_CAPSULE_ENVIRONMENT_SHA256S[capsule_id],
            required_distributions=OFFICIAL_CAPSULE_REQUIREMENTS[capsule_id],
        )
        for capsule_id in DEFAULT_COREBENCH_CAPSULE_IDS
    }


class CoreBenchSourceManifest(FrozenModel):
    schema_version: Literal[1] = 1
    benchmark: Literal["AstaBench CORE-Bench-Hard"] = "AstaBench CORE-Bench-Hard"
    difficulty: Literal["hard"] = "hard"
    split: Literal["validation"] = "validation"
    upstream_split: Literal["train"] = "train"
    astabench_repository_url: str = ASTABENCH_REPOSITORY_URL
    astabench_version: str = ASTABENCH_VERSION
    astabench_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    astabench_core_wrapper_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inspect_evals_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    inspect_evals_scorer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inspect_evals_utils_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corebench_repository_url: str = COREBENCH_REPOSITORY_URL
    dataset_id: str = COREBENCH_DATASET_ID
    dataset_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    annotation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    annotation_rows: int = Field(default=45, gt=0)
    benchmark_license: Literal["MIT"] = "MIT"
    astabench_license: Literal["Apache-2.0"] = "Apache-2.0"
    inspect_evals_license: Literal["MIT"] = "MIT"
    test_data_policy: Literal["never_downloaded-or-decrypted"] = "never_downloaded-or-decrypted"
    capsule_contract_policy: Literal["frozen-reviewed", "explicit-custom"] = "explicit-custom"
    reviewed_capsules: dict[str, CoreBenchCapsuleContract] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reviewed_contracts_are_complete(self) -> "CoreBenchSourceManifest":
        for capsule_id in self.reviewed_capsules:
            if not _CAPSULE_ID.fullmatch(capsule_id):
                raise ValueError("CORE-Bench reviewed capsule ID is malformed")
        if self.capsule_contract_policy == "frozen-reviewed" and set(
            self.reviewed_capsules
        ) != set(DEFAULT_COREBENCH_CAPSULE_IDS):
            raise ValueError("the frozen official source requires every default capsule contract")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)

    @classmethod
    def official_validation(cls) -> "CoreBenchSourceManifest":
        return cls(
            astabench_commit=ASTABENCH_COMMIT,
            astabench_core_wrapper_sha256=ASTABENCH_CORE_WRAPPER_SHA256,
            inspect_evals_commit=INSPECT_EVALS_COMMIT,
            inspect_evals_scorer_sha256=INSPECT_EVALS_SCORER_SHA256,
            inspect_evals_utils_sha256=INSPECT_EVALS_UTILS_SHA256,
            dataset_revision=COREBENCH_DATASET_REVISION,
            annotation_sha256=COREBENCH_TRAIN_JSON_SHA256,
            capsule_contract_policy="frozen-reviewed",
            reviewed_capsules=_official_capsule_contracts(),
        )


class CoreBenchInstance(FrozenModel):
    field: str = Field(min_length=1)
    language: Literal["Python", "R"]
    capsule_title: str = Field(min_length=1)
    capsule_id: str = Field(pattern=r"^capsule-[0-9]{7}$")
    task_prompt: str = Field(min_length=1)
    results: tuple[dict[str, Any], ...] = Field(min_length=2)
    capsule_doi: str = Field(min_length=1)

    @model_validator(mode="after")
    def _results_have_one_stable_schema(self) -> "CoreBenchInstance":
        keys = tuple(self.results[0])
        if not keys or len(keys) != len(set(keys)):
            raise ValueError("CORE-Bench result questions must be non-empty and unique")
        if any(tuple(row) != keys for row in self.results[1:]):
            raise ValueError("CORE-Bench ground-truth trials must share exact ordered questions")
        for row in self.results:
            if any(not isinstance(value, (int, float, str, list)) for value in row.values()):
                raise ValueError("CORE-Bench ground truth contains an unsupported answer type")
        return self

    @property
    def public_questions(self) -> tuple[str, ...]:
        return tuple(self.results[0])

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class CoreBenchSubsetManifest(FrozenModel):
    schema_version: Literal[1] = 1
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capsule_ids: tuple[str, ...] = Field(min_length=1)
    code_license_by_capsule: dict[str, Literal["MIT"]]
    data_license_by_capsule: dict[str, Literal["CC0-1.0"]]
    selection_policy: Literal["explicit-license-audited-no-best-of-n"] = (
        "explicit-license-audited-no-best-of-n"
    )

    @model_validator(mode="after")
    def _selection_is_unique_and_licensed(self) -> "CoreBenchSubsetManifest":
        if len(self.capsule_ids) != len(set(self.capsule_ids)):
            raise ValueError("CORE-Bench subset IDs must be unique")
        expected = set(self.capsule_ids)
        if set(self.code_license_by_capsule) != expected or set(self.data_license_by_capsule) != expected:
            raise ValueError("every CORE-Bench capsule requires explicit code and data licenses")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)


class CoreBenchAssetReceipt(FrozenModel):
    schema_version: Literal[1] = 1
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instance: CoreBenchInstance
    source_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_archive_bytes: int = Field(gt=0)
    source_archive_files: int = Field(gt=0)
    source_expanded_bytes: int = Field(gt=0)
    public_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    public_archive_bytes: int = Field(gt=0)
    public_file_count: int = Field(gt=0)
    public_expanded_bytes: int = Field(gt=0)
    public_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_license: Literal["MIT"] = "MIT"
    data_license: Literal["CC0-1.0"] = "CC0-1.0"
    code_license_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_license_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_dockerfile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sanitized_removed: tuple[str, ...] = (
        "results",
        "REPRODUCING.md",
        "environment",
        ".DS_Store",
        "reproduction_artifacts",
    )
    capsule_assets_redistributable: Literal[False] = False

    @property
    def hidden_sha256(self) -> str:
        return _sha256_bytes(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _canonical_bytes(self)

    @property
    def gold_bytes(self) -> bytes:
        return _canonical_bytes(list(self.instance.results))


class CoreBenchHarnessManifest(FrozenModel):
    schema_version: Literal[1] = 1
    harness_id: Literal["aletheia-asta-corebench-hard-isolated-v1"] = (
        "aletheia-asta-corebench-hard-isolated-v1"
    )
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scorer_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scorer_entrypoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_environment: dict[str, str] = Field(min_length=1)
    supported_capsule_requirements: dict[str, tuple[str, ...]] = Field(min_length=1)
    reproduction_runs: int = Field(default=2, ge=2, le=5)
    candidate_wall_time_s: int = Field(default=900, gt=0)
    candidate_cpu_seconds: int = Field(default=900, gt=0)
    candidate_memory_mb: int = Field(default=4096, gt=0)
    scorer_wall_time_s: int = Field(default=60, gt=0)
    scorer_cpu_seconds: int = Field(default=30, gt=0)
    scorer_memory_mb: int = Field(default=512, gt=0)
    max_program_bytes: int = Field(default=1 << 20, gt=0)
    max_report_bytes: int = Field(default=1 << 20, gt=0)
    max_artifact_files: int = Field(default=2_000, gt=0)
    max_artifact_bytes: int = Field(default=1 << 30, gt=0)
    network_mode: Literal["none"] = "none"
    results_policy: Literal["never-mounted-to-candidate"] = "never-mounted-to-candidate"
    aggregation_policy: Literal["all-runs-retained-no-best-of-n"] = (
        "all-runs-retained-no-best-of-n"
    )

    @model_validator(mode="after")
    def _environment_contract_is_complete(self) -> "CoreBenchHarnessManifest":
        if self.candidate_environment.get("python") in {None, "not-installed"}:
            raise ValueError("CORE-Bench candidate image requires Python")
        for capsule_id, packages in self.supported_capsule_requirements.items():
            if not _CAPSULE_ID.fullmatch(capsule_id) or not packages or len(packages) != len(set(packages)):
                raise ValueError("CORE-Bench environment contracts must be explicit and unique")
            missing = [
                package
                for package in packages
                if self.candidate_environment.get(package) in {None, "not-installed"}
            ]
            if missing:
                raise ValueError(f"candidate image lacks {capsule_id} requirements: {missing}")
        return self

    @property
    def manifest_sha256(self) -> str:
        return content_sha256(self)

    def assert_capsule_supported(self, capsule_id: str) -> None:
        if capsule_id not in self.supported_capsule_requirements:
            raise ValueError(f"CORE-Bench capsule {capsule_id} has no reviewed environment contract")


class CoreBenchHarnessResult(FrozenModel):
    schema_version: Literal[1] = 1
    capsule_id: str = Field(pattern=r"^capsule-[0-9]{7}$")
    run_index: int = Field(ge=0)
    candidate_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scorer_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    program_returncode: int | None = None
    program_exit_reason: ExecutionExitReason
    program_timed_out: bool = False
    program_wall_time_s: float = Field(ge=0)
    program_log_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_valid: bool
    report_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    report_bytes: int = Field(default=0, ge=0)
    artifact_tree_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_file_count: int = Field(default=0, ge=0)
    artifact_total_bytes: int = Field(default=0, ge=0)
    correct_written_answers: int = Field(default=0, ge=0)
    correct_vision_answers: int = Field(default=0, ge=0)
    total_written_questions: int = Field(default=0, ge=0)
    total_vision_questions: int = Field(default=0, ge=0)
    correct: bool = False
    evaluator_wall_time_s: float | None = Field(default=None, ge=0)
    evaluator_log_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _result_is_consistent(self) -> "CoreBenchHarnessResult":
        if self.program_timed_out != (self.program_exit_reason is ExecutionExitReason.WALL_TIME_LIMIT):
            raise ValueError("program timeout must match its exit reason")
        if self.report_sha256 is None and self.report_bytes:
            raise ValueError("report bytes require a report digest")
        if self.artifact_tree_sha256 is None and (self.artifact_file_count or self.artifact_total_bytes):
            raise ValueError("artifact statistics require a tree digest")
        if self.correct and (
            not self.report_valid
            or self.correct_written_answers != self.total_written_questions
            or self.correct_vision_answers != self.total_vision_questions
        ):
            raise ValueError("correct CORE-Bench results must answer every question")
        return self

    @property
    def receipt_sha256(self) -> str:
        return content_sha256(self)


class CoreBenchHarness(Protocol):
    @property
    def manifest(self) -> CoreBenchHarnessManifest: ...

    def evaluate(
        self, *, receipt: CoreBenchAssetReceipt, program: bytes, run_index: int
    ) -> CoreBenchHarnessResult: ...


class CoreBenchAdapter:
    def __init__(self, source: CoreBenchSourceManifest | None = None) -> None:
        self.source = source or CoreBenchSourceManifest.official_validation()

    def load_instances(self, annotation_path: Path) -> tuple[CoreBenchInstance, ...]:
        path = Path(annotation_path)
        if _sha256_file(path) != self.source.annotation_sha256:
            raise ValueError("CORE-Bench annotation hash does not match the frozen source")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list) or len(payload) != self.source.annotation_rows:
            raise ValueError("CORE-Bench annotation row count does not match its manifest")
        instances = tuple(CoreBenchInstance.model_validate(item) for item in payload)
        ids = [instance.capsule_id for instance in instances]
        if len(ids) != len(set(ids)):
            raise ValueError("CORE-Bench annotation contains duplicate capsule IDs")
        return instances

    def select_subset(
        self,
        instances: Sequence[CoreBenchInstance],
        *,
        capsule_ids: Sequence[str] = DEFAULT_COREBENCH_CAPSULE_IDS,
    ) -> tuple[tuple[CoreBenchInstance, ...], CoreBenchSubsetManifest]:
        requested = tuple(capsule_ids)
        if not requested or len(requested) != len(set(requested)):
            raise ValueError("CORE-Bench subset IDs must be non-empty and unique")
        by_id = {instance.capsule_id: instance for instance in instances}
        missing = set(requested) - set(by_id)
        if missing:
            raise ValueError(f"CORE-Bench subset IDs are absent: {sorted(missing)}")
        if self.source.capsule_contract_policy == "frozen-reviewed":
            unreviewed = set(requested) - set(self.source.reviewed_capsules)
            if unreviewed:
                raise ValueError(f"CORE-Bench capsule IDs lack frozen contracts: {sorted(unreviewed)}")
        return (
            tuple(by_id[capsule_id] for capsule_id in requested),
            CoreBenchSubsetManifest(
                source_manifest_sha256=self.source.manifest_sha256,
                capsule_ids=requested,
                code_license_by_capsule={capsule_id: "MIT" for capsule_id in requested},
                data_license_by_capsule={capsule_id: "CC0-1.0" for capsule_id in requested},
            ),
        )

    def freeze_capsule(
        self,
        *,
        instance: CoreBenchInstance,
        archive_path: Path,
        asset_root: Path,
    ) -> CoreBenchAssetReceipt:
        archive_path = Path(archive_path).resolve(strict=True)
        expected_root = instance.capsule_id
        source_files, source_bytes = _archive_inventory(archive_path, expected_root=expected_root)
        prefix = f"{expected_root}/"
        code_license = _read_tar_member(archive_path, prefix + "code/LICENSE")
        data_license = _read_tar_member(archive_path, prefix + "data/LICENSE")
        environment = _read_tar_member(archive_path, prefix + "environment/Dockerfile")
        if not code_license.startswith(b"MIT License"):
            raise ValueError(f"{instance.capsule_id} code license is not the reviewed MIT license")
        if not data_license.startswith(b"CC0 1.0 Universal"):
            raise ValueError(f"{instance.capsule_id} data license is not the reviewed CC0 license")
        source_archive_sha256 = _sha256_file(archive_path)
        code_license_sha256 = _sha256_bytes(code_license)
        data_license_sha256 = _sha256_bytes(data_license)
        environment_sha256 = _sha256_bytes(environment)
        contract = self.source.reviewed_capsules.get(instance.capsule_id)
        if contract is None and self.source.capsule_contract_policy == "frozen-reviewed":
            raise ValueError(f"{instance.capsule_id} has no frozen official capsule contract")
        if contract is not None:
            observed = (
                source_archive_sha256,
                archive_path.stat().st_size,
                code_license_sha256,
                data_license_sha256,
                environment_sha256,
            )
            expected = (
                contract.archive_sha256,
                contract.archive_bytes,
                contract.code_license_sha256,
                contract.data_license_sha256,
                contract.environment_dockerfile_sha256,
            )
            if observed != expected:
                raise ValueError(
                    f"{instance.capsule_id} archive or license/environment bytes differ from "
                    "the frozen official contract"
                )

        asset_root = Path(asset_root).resolve(strict=False)
        public_dir = asset_root / "public_assets" / "corebench" / self.source.manifest_sha256
        public_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = public_dir / f"{instance.capsule_id}.tar.gz"
        with tempfile.TemporaryDirectory(prefix="corebench-sanitize-", dir=asset_root) as temporary:
            expanded = Path(temporary) / "capsule"
            _safe_extract_capsule(archive_path, expanded, expected_root=expected_root)
            for name in (
                "results",
                "REPRODUCING.md",
                "environment",
                ".DS_Store",
                "reproduction_artifacts",
            ):
                candidate = expanded / name
                if candidate.is_dir():
                    shutil.rmtree(candidate)
                elif candidate.exists():
                    candidate.unlink()
            tree_sha256, public_files, public_expanded_bytes = _tree_manifest(expanded)
            descriptor, temporary_archive = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            os.close(descriptor)
            Path(temporary_archive).unlink()
            try:
                archive_sha256, archive_bytes, written_files, written_bytes = _write_deterministic_tar(
                    expanded, Path(temporary_archive)
                )
                if (written_files, written_bytes) != (public_files, public_expanded_bytes):
                    raise ValueError("sanitized CORE-Bench archive statistics changed while freezing")
                if target.exists():
                    if _sha256_file(target) != archive_sha256:
                        raise ValueError("existing CORE-Bench public asset has different bytes")
                    Path(temporary_archive).unlink()
                else:
                    os.chmod(temporary_archive, 0o400)
                    os.replace(temporary_archive, target)
            finally:
                try:
                    Path(temporary_archive).unlink()
                except FileNotFoundError:
                    pass
        return CoreBenchAssetReceipt(
            source_manifest_sha256=self.source.manifest_sha256,
            instance=instance,
            source_archive_sha256=source_archive_sha256,
            source_archive_bytes=archive_path.stat().st_size,
            source_archive_files=source_files,
            source_expanded_bytes=source_bytes,
            public_archive_sha256=archive_sha256,
            public_archive_bytes=archive_bytes,
            public_file_count=public_files,
            public_expanded_bytes=public_expanded_bytes,
            public_tree_sha256=tree_sha256,
            code_license_sha256=code_license_sha256,
            data_license_sha256=data_license_sha256,
            environment_dockerfile_sha256=environment_sha256,
        )

    def build_task(
        self,
        *,
        receipt: CoreBenchAssetReceipt,
        scorer: "CoreBenchScorer",
        resource_budget: ResourceBudget,
        test_access_limit: int = 1,
    ) -> EvaluationTask:
        if receipt.source_manifest_sha256 != self.source.manifest_sha256:
            raise ValueError("CORE-Bench receipt is bound to another source release")
        instance = receipt.instance
        questions = "\n".join(f"- {question}" for question in instance.public_questions)
        prompt = (
            "AstaBench CORE-Bench-Hard validation task\n"
            f"Capsule: {instance.capsule_id} — {instance.capsule_title}\n"
            f"Field/language: {instance.field} / {instance.language}\n\n"
            f"Reproduction objective: {instance.task_prompt}\n\n"
            f"Answer every question exactly as a key in report.json:\n{questions}\n\n"
            "The sanitized repository is available at capsule/. Its original results, "
            "REPRODUCING.md, and environment directory are intentionally absent. Submit one "
            "UTF-8 Python artifact named kind 'reproduction_program'. The independent harness "
            "executes it twice from a fresh copy with network disabled. It must reproduce the "
            "computation, write capsule/report.json, and leave at least one additional regular "
            "file below capsule/reproduction_artifacts/. Do not read or infer hidden benchmark "
            "answer files; disclose any benchmark-answer or training overlap."
        )
        public_asset = EvaluationPublicAsset(
            asset_id=instance.capsule_id,
            evaluator_ref=(
                "evaluator://public/corebench/"
                f"{self.source.manifest_sha256}/{instance.capsule_id}.tar.gz"
            ),
            sha256=receipt.public_archive_sha256,
            bytes=receipt.public_archive_bytes,
            file_count=receipt.public_file_count,
            expanded_bytes=receipt.public_expanded_bytes,
            mount_path="capsule",
        )
        return EvaluationTask(
            task_id=f"corebench-hard-{instance.capsule_id.removeprefix('capsule-')}",
            version=f"validation-{self.source.dataset_revision[:12]}-adapter-v1",
            layer=EvalLayer.SCIENTIFIC_REPRODUCTION,
            public_prompt=prompt,
            hidden_asset_ref=(
                "evaluator://hidden/corebench/"
                f"{self.source.manifest_sha256}/{instance.capsule_id}.json"
            ),
            hidden_asset_sha256=receipt.hidden_sha256,
            resource_budget=resource_budget,
            allowed_tools=("python", "shell", "filesystem"),
            public_assets=(public_asset,),
            expected_artifacts=(
                ArtifactRequirement(
                    kind="reproduction_program", media_type="text/x-python", max_bytes=1 << 20
                ),
            ),
            scorer_ref="evaluator://scorers/asta-corebench-hard-isolated-v1",
            scorer_sha256=scorer.scorer_sha256,
            contamination_policy=ContaminationPolicy(
                forbidden_sources=(
                    "CORE-Bench train/test answer annotations",
                    "capsule results directory",
                    "Asta/inspect_evals scorer implementation",
                ),
                disclose_training_overlap=True,
                test_access_limit=test_access_limit,
            ),
        )

    def build_suite(
        self,
        *,
        tasks: Sequence[EvaluationTask],
        subset_manifest: CoreBenchSubsetManifest,
        scorer: "CoreBenchScorer",
    ) -> EvaluationSuite:
        expected = tuple(
            f"corebench-hard-{capsule_id.removeprefix('capsule-')}"
            for capsule_id in subset_manifest.capsule_ids
        )
        if tuple(task.task_id for task in tasks) != expected:
            raise ValueError("CORE-Bench suite task order differs from its subset manifest")
        if any(task.scorer_sha256 != scorer.scorer_sha256 for task in tasks):
            raise ValueError("CORE-Bench suite tasks are not bound to the loaded scorer")
        for capsule_id in subset_manifest.capsule_ids:
            scorer.harness.manifest.assert_capsule_supported(capsule_id)
        scoring_policy_sha256 = content_sha256(
            {
                "policy_id": "asta-corebench-hard-objective-artifact-reproducible-v1",
                "source_manifest_sha256": self.source.manifest_sha256,
                "subset_manifest_sha256": subset_manifest.manifest_sha256,
                "scorer_sha256": scorer.scorer_sha256,
                "scientific_success": "all official questions correct and artifact present",
                "reproducibility": "two exact report and artifact-tree matches",
                "aggregation": "all-runs-retained-no-best-of-n",
            }
        )
        return EvaluationSuite(
            suite_id="asta-corebench-hard-validation-mit-cc0-mini",
            version=f"{self.source.dataset_revision[:12]}-adapter-v1",
            task_manifest_sha256s=tuple(task.manifest_sha256 for task in tasks),
            scoring_policy_sha256=scoring_policy_sha256,
        )

    @staticmethod
    def stage_hidden_asset(
        *, evaluator_root: Path, task: EvaluationTask, receipt: CoreBenchAssetReceipt
    ) -> Path:
        prefix = "evaluator://hidden/"
        relative = PurePosixPath(task.hidden_asset_ref.removeprefix(prefix))
        if not task.hidden_asset_ref.startswith(prefix) or relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise ValueError("CORE-Bench hidden asset ref escaped evaluator storage")
        target = Path(evaluator_root) / "hidden_assets" / Path(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            if _sha256_file(target) != task.hidden_asset_sha256:
                raise ValueError("existing CORE-Bench hidden asset has different bytes")
            return target
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(receipt.to_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o400)
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return target


class DockerCoreBenchHarness:
    def __init__(
        self,
        *,
        manifest: CoreBenchHarnessManifest,
        public_asset_root: Path,
        scratch_root: Path,
    ) -> None:
        self._manifest = manifest
        self.public_asset_root = Path(public_asset_root).resolve(strict=True)
        self.scratch_root = Path(scratch_root).resolve(strict=False)
        self.scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.entrypoint = Path(__file__).with_name("corebench_scorer_entrypoint.py").resolve(strict=True)
        if _sha256_file(self.entrypoint) != manifest.scorer_entrypoint_sha256:
            raise ValueError("CORE-Bench scorer entrypoint differs from its manifest")

    @property
    def manifest(self) -> CoreBenchHarnessManifest:
        return self._manifest

    @classmethod
    def from_image_refs(
        cls,
        *,
        candidate_image_ref: str,
        scorer_image_ref: str,
        source_manifest_sha256: str,
        public_asset_root: Path,
        scratch_root: Path,
        supported_capsule_requirements: dict[str, tuple[str, ...]],
        reproduction_runs: int = 2,
        **resource_limits: int,
    ) -> "DockerCoreBenchHarness":
        from aletheia.coder.executor import resolve_docker_image

        scratch = Path(scratch_root).resolve(strict=False)
        scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        candidate_image_id = resolve_docker_image(candidate_image_ref)
        scorer_image_id = resolve_docker_image(scorer_image_ref)
        environment = _probe_docker_environment(candidate_image_id, scratch_root=scratch)
        manifest = CoreBenchHarnessManifest(
            source_manifest_sha256=source_manifest_sha256,
            candidate_image_id=candidate_image_id,
            scorer_image_id=scorer_image_id,
            scorer_entrypoint_sha256=scorer_entrypoint_sha256(),
            candidate_environment=environment,
            supported_capsule_requirements=supported_capsule_requirements,
            reproduction_runs=reproduction_runs,
            **resource_limits,
        )
        return cls(
            manifest=manifest,
            public_asset_root=public_asset_root,
            scratch_root=scratch,
        )

    def _verify_public_asset(self, receipt: CoreBenchAssetReceipt) -> Path:
        path = (
            self.public_asset_root
            / "corebench"
            / receipt.source_manifest_sha256
            / f"{receipt.instance.capsule_id}.tar.gz"
        )
        try:
            digest = _sha256_file(path, max_bytes=receipt.public_archive_bytes)
        except (OSError, ValueError) as exc:
            raise EvaluationScorerInfrastructureError(str(exc)) from exc
        if digest != receipt.public_archive_sha256 or path.stat().st_size != receipt.public_archive_bytes:
            raise EvaluationScorerInfrastructureError(
                "CORE-Bench public capsule differs from the frozen receipt"
            )
        return path

    @staticmethod
    def _candidate_artifacts(capsule: Path, manifest: CoreBenchHarnessManifest) -> tuple[str | None, int, int]:
        root = capsule / "reproduction_artifacts"
        if not root.exists():
            return None, 0, 0
        try:
            digest, files, size = _tree_manifest(root)
        except ValueError:
            return None, 0, 0
        if files > manifest.max_artifact_files or size > manifest.max_artifact_bytes:
            return None, files, size
        return digest, files, size

    def evaluate(
        self, *, receipt: CoreBenchAssetReceipt, program: bytes, run_index: int
    ) -> CoreBenchHarnessResult:
        if len(program) > self.manifest.max_program_bytes:
            return CoreBenchHarnessResult(
                capsule_id=receipt.instance.capsule_id,
                run_index=run_index,
                candidate_image_id=self.manifest.candidate_image_id,
                scorer_image_id=self.manifest.scorer_image_id,
                program_returncode=None,
                program_exit_reason=ExecutionExitReason.RESOURCE_LIMIT,
                program_wall_time_s=0,
                program_log_sha256=_sha256_bytes(b"program-byte-limit"),
                report_valid=False,
            )
        public_archive = self._verify_public_asset(receipt)
        temporary = tempfile.mkdtemp(prefix="corebench-attempt-", dir=self.scratch_root)
        run_root = Path(temporary).resolve()
        try:
            capsule = run_root / "capsule"
            from aletheia.evals.runner import IndependentEvaluationRunner

            IndependentEvaluationRunner._safe_extract_public_archive(
                public_archive.read_bytes(),
                destination=capsule,
                expected_files=receipt.public_file_count,
                expected_bytes=receipt.public_expanded_bytes,
            )
            tree_sha256, files, size = _tree_manifest(capsule)
            if (tree_sha256, files, size) != (
                receipt.public_tree_sha256,
                receipt.public_file_count,
                receipt.public_expanded_bytes,
            ):
                raise EvaluationScorerInfrastructureError(
                    "CORE-Bench public capsule expansion differs from the frozen receipt"
                )
            program_path = run_root / "reproduce.py"
            program_path.write_bytes(program)
            os.chmod(program_path, 0o444)
            candidate_name = f"aletheia-corebench-candidate-{uuid.uuid4().hex[:16]}"
            command = hardened_docker_command(
                run_root,
                image_id=self.manifest.candidate_image_id,
                container_name=candidate_name,
                container_dir="/testbed",
                writable=True,
                command=["python", "/testbed/reproduce.py"],
                memory_mb=self.manifest.candidate_memory_mb,
                cpus=max(0.01, self.manifest.candidate_cpu_seconds / self.manifest.candidate_wall_time_s),
                cpu_seconds=self.manifest.candidate_cpu_seconds,
                environment={"ALETHEIA_EVAL_SEED": "0", "PYTHONHASHSEED": "0"},
                include_aletheia_pythonpath=False,
            )
            candidate = run_hardened_container(
                command,
                container_name=candidate_name,
                timeout_s=self.manifest.candidate_wall_time_s,
                image_id=self.manifest.candidate_image_id,
            )
            program_log_sha256 = _sha256_bytes(candidate.output.encode("utf-8"))
            if candidate.error is not None or docker_execution_is_infrastructure_failure(candidate):
                raise EvaluationScorerInfrastructureError(
                    f"CORE-Bench candidate container failed: {candidate.error or candidate.output[-512:]}"
                )
            if candidate.timed_out:
                exit_reason = ExecutionExitReason.WALL_TIME_LIMIT
            elif candidate.returncode in {-9, 137, 152}:
                exit_reason = ExecutionExitReason.RESOURCE_LIMIT
            elif candidate.returncode != 0:
                exit_reason = ExecutionExitReason.PROCESS_ERROR
            else:
                exit_reason = ExecutionExitReason.COMPLETED

            report_path = capsule / "report.json"
            report_sha256: str | None = None
            report_bytes = 0
            if report_path.exists():
                try:
                    resolved = report_path.resolve(strict=True)
                    if report_path.is_symlink() or capsule not in resolved.parents:
                        raise ValueError("CORE-Bench report escaped the capsule")
                    report_bytes = report_path.stat().st_size
                    report_sha256 = _sha256_file(report_path, max_bytes=self.manifest.max_report_bytes)
                except (OSError, ValueError):
                    exit_reason = ExecutionExitReason.RESOURCE_LIMIT
                    report_sha256 = None
                    report_bytes = 0
            artifact_sha256, artifact_files, artifact_bytes = self._candidate_artifacts(
                capsule, self.manifest
            )
            if artifact_files > self.manifest.max_artifact_files or artifact_bytes > self.manifest.max_artifact_bytes:
                exit_reason = ExecutionExitReason.RESOURCE_LIMIT

            if exit_reason is not ExecutionExitReason.COMPLETED or report_sha256 is None:
                return CoreBenchHarnessResult(
                    capsule_id=receipt.instance.capsule_id,
                    run_index=run_index,
                    candidate_image_id=self.manifest.candidate_image_id,
                    scorer_image_id=self.manifest.scorer_image_id,
                    program_returncode=candidate.returncode,
                    program_exit_reason=exit_reason,
                    program_timed_out=candidate.timed_out,
                    program_wall_time_s=candidate.wall_time_s,
                    program_log_sha256=program_log_sha256,
                    report_valid=False,
                    report_sha256=report_sha256,
                    report_bytes=report_bytes,
                    artifact_tree_sha256=artifact_sha256,
                    artifact_file_count=artifact_files,
                    artifact_total_bytes=artifact_bytes,
                )

            evaluator_work = run_root / "evaluator_work"
            evaluator_work.mkdir()
            receipt_dir = run_root / "evaluator_receipt"
            receipt_dir.mkdir(mode=0o700)
            gold_dir = run_root / "gold"
            gold_dir.mkdir(mode=0o700)
            gold_path = gold_dir / "answers.json"
            gold_path.write_bytes(receipt.gold_bytes)
            os.chmod(gold_path, 0o400)
            scorer_name = f"aletheia-corebench-scorer-{uuid.uuid4().hex[:16]}"
            scorer_command = hardened_docker_command(
                evaluator_work,
                image_id=self.manifest.scorer_image_id,
                container_name=scorer_name,
                container_dir="/work",
                writable=True,
                command=["python", "/evaluator/corebench_scorer_entrypoint.py"],
                additional_mounts=(
                    (self.entrypoint.parent, "/evaluator", False),
                    (report_path, "/candidate/report.json", False),
                    (gold_dir, "/gold", False),
                    (receipt_dir, "/receipt", True),
                ),
                memory_mb=self.manifest.scorer_memory_mb,
                cpus=max(0.01, self.manifest.scorer_cpu_seconds / self.manifest.scorer_wall_time_s),
                cpu_seconds=self.manifest.scorer_cpu_seconds,
                environment={"COREBENCH_GOLD_PATH": "/gold/answers.json"},
                include_aletheia_pythonpath=False,
            )
            result_path = receipt_dir / "result.json"
            evaluation = run_hardened_container(
                scorer_command,
                container_name=scorer_name,
                timeout_s=self.manifest.scorer_wall_time_s,
                image_id=self.manifest.scorer_image_id,
                trusted_terminal_receipt=result_path,
            )
            if (
                evaluation.error is not None
                or docker_execution_is_infrastructure_failure(evaluation)
                or evaluation.timed_out
                or not evaluation.trusted_terminal_receipt_observed
            ):
                raise EvaluationScorerInfrastructureError(
                    f"CORE-Bench evaluator container failed: {evaluation.error or evaluation.output[-512:]}"
                )
            if not result_path.is_file() or result_path.is_symlink():
                raise EvaluationScorerInfrastructureError(
                    f"CORE-Bench trusted evaluator did not issue a receipt: {evaluation.output[-512:]}"
                )
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            expected_keys = {
                "correct_written_answers", "correct_vision_answers",
                "total_written_questions", "total_vision_questions", "correct",
                "report_valid", "report_sha256", "report_bytes",
            }
            if set(payload) != expected_keys or payload["report_sha256"] != report_sha256 or payload["report_bytes"] != report_bytes:
                raise EvaluationScorerInfrastructureError("CORE-Bench evaluator receipt is malformed")
            return CoreBenchHarnessResult(
                capsule_id=receipt.instance.capsule_id,
                run_index=run_index,
                candidate_image_id=self.manifest.candidate_image_id,
                scorer_image_id=self.manifest.scorer_image_id,
                program_returncode=candidate.returncode,
                program_exit_reason=exit_reason,
                program_wall_time_s=candidate.wall_time_s,
                program_log_sha256=program_log_sha256,
                artifact_tree_sha256=artifact_sha256,
                artifact_file_count=artifact_files,
                artifact_total_bytes=artifact_bytes,
                evaluator_wall_time_s=evaluation.wall_time_s,
                evaluator_log_sha256=_sha256_bytes(evaluation.output.encode("utf-8")),
                **payload,
            )
        finally:
            shutil.rmtree(run_root, ignore_errors=True)


class CoreBenchScorer:
    def __init__(self, *, harness: CoreBenchHarness, source_manifest_sha256: str) -> None:
        if not _DIGEST.fullmatch(source_manifest_sha256):
            raise ValueError("CORE-Bench scorer requires a source manifest SHA-256")
        self.harness = harness
        self.source_manifest_sha256 = source_manifest_sha256
        self._scorer_sha256 = content_sha256(
            {
                "scorer_id": "aletheia-asta-corebench-hard-scorer-v1",
                "implementation_sha256": _sha256_file(Path(__file__)),
                "entrypoint_sha256": scorer_entrypoint_sha256(),
                "upstream_scorer_sha256": INSPECT_EVALS_SCORER_SHA256,
                "upstream_utils_sha256": INSPECT_EVALS_UTILS_SHA256,
                "harness_manifest_sha256": harness.manifest.manifest_sha256,
                "source_manifest_sha256": source_manifest_sha256,
                "artifact_policy": "non-empty-exact-tree-across-all-runs-v1",
            }
        )

    @property
    def scorer_sha256(self) -> str:
        return self._scorer_sha256

    @staticmethod
    def _contaminated(program: str, submission: EvaluationSubmission) -> bool:
        if submission.declared_contamination or COREBENCH_CANARY in program:
            return True
        return any(pattern.search(program) for pattern in _FORBIDDEN_PROGRAM_REFERENCES)

    def score(
        self,
        *,
        task: EvaluationTask,
        hidden_asset: bytes,
        submission: EvaluationSubmission,
        artifacts: dict[str, bytes],
    ) -> EvaluationScore:
        try:
            receipt = CoreBenchAssetReceipt.model_validate_json(hidden_asset)
        except Exception as exc:
            raise EvaluationScorerInfrastructureError(
                "CORE-Bench hidden asset receipt is malformed"
            ) from exc
        if receipt.source_manifest_sha256 != self.source_manifest_sha256:
            raise EvaluationScorerInfrastructureError(
                "CORE-Bench receipt is bound to a different source release"
            )
        expected_id = f"corebench-hard-{receipt.instance.capsule_id.removeprefix('capsule-')}"
        if task.task_id != expected_id:
            raise EvaluationScorerInfrastructureError(
                "CORE-Bench task and hidden receipt capsule do not match"
            )
        program_bytes = artifacts.get("reproduction_program")
        if program_bytes is None:
            return EvaluationScore(invalid_reasons=(InvalidReason.MISSING_ARTIFACT,))
        try:
            program = program_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return EvaluationScore(invalid_reasons=(InvalidReason.PROTOCOL_BREACH,))
        if self._contaminated(program, submission):
            return EvaluationScore(
                evidence_sha256s={"submitted_program": _sha256_bytes(program_bytes)},
                invalid_reasons=(InvalidReason.CONTAMINATION,),
            )

        results: list[CoreBenchHarnessResult] = []
        for index in range(self.harness.manifest.reproduction_runs):
            result = self.harness.evaluate(receipt=receipt, program=program_bytes, run_index=index)
            if (
                result.capsule_id != receipt.instance.capsule_id
                or result.run_index != index
                or result.candidate_image_id != self.harness.manifest.candidate_image_id
                or result.scorer_image_id != self.harness.manifest.scorer_image_id
            ):
                raise EvaluationScorerInfrastructureError(
                    "CORE-Bench harness result is not bound to its frozen run"
                )
            results.append(result)
        evidence_sha256s = {
            "submitted_program": _sha256_bytes(program_bytes),
            **{f"harness_run_{item.run_index}": item.receipt_sha256 for item in results},
        }
        evidence_objects = {
            f"harness_run_{item.run_index}": item.model_dump(mode="json", exclude_none=True)
            for item in results
        }
        if any(
            item.program_exit_reason
            in {ExecutionExitReason.WALL_TIME_LIMIT, ExecutionExitReason.RESOURCE_LIMIT}
            for item in results
        ):
            return EvaluationScore(
                objective_scores={"reproducible": 0.0},
                evidence_sha256s=evidence_sha256s,
                evidence_objects=evidence_objects,
                invalid_reasons=(InvalidReason.RESOURCE_LIMIT,),
            )
        reproducibility_keys = {
            (
                item.program_exit_reason,
                item.report_valid,
                item.report_sha256,
                item.artifact_tree_sha256,
                item.artifact_file_count,
                item.artifact_total_bytes,
                item.correct,
                item.correct_written_answers,
                item.correct_vision_answers,
            )
            for item in results
        }
        if len(reproducibility_keys) != 1:
            return EvaluationScore(
                objective_scores={"reproducible": 0.0},
                evidence_sha256s=evidence_sha256s,
                evidence_objects=evidence_objects,
                invalid_reasons=(InvalidReason.NON_REPRODUCIBLE,),
            )
        result = results[0]
        runnable = result.program_exit_reason is ExecutionExitReason.COMPLETED
        artifact_present = result.artifact_tree_sha256 is not None and result.artifact_file_count > 0
        total_questions = result.total_written_questions + result.total_vision_questions
        correct_questions = result.correct_written_answers + result.correct_vision_answers
        question_accuracy = correct_questions / total_questions if total_questions else 0.0
        return EvaluationScore(
            objective_scores={
                "runnable": float(runnable),
                "valid_report": float(result.report_valid),
                "question_accuracy": question_accuracy,
                "artifact_present": float(artifact_present),
                "reproducible": 1.0,
            },
            evidence_sha256s=evidence_sha256s,
            evidence_objects=evidence_objects,
            scientific_success=runnable and result.correct and artifact_present,
        )


def scorer_entrypoint_sha256() -> str:
    return _sha256_file(Path(__file__).with_name("corebench_scorer_entrypoint.py"))


def _probe_docker_environment(image_ref: str, *, scratch_root: Path) -> dict[str, str]:
    from aletheia.coder.executor import resolve_docker_image

    image_id = resolve_docker_image(image_ref)
    probe = """
import importlib.metadata as metadata
import json
import shutil
import sys

names = ["numpy", "pandas", "scikit-learn", "networkx", "jupyter", "nbconvert", "seaborn"]
out = {"python": sys.version.split()[0], "bash": "available" if shutil.which("bash") else "not-installed"}
for name in names:
    try:
        out[name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        out[name] = "not-installed"
print(json.dumps(out, sort_keys=True))
"""
    root = Path(tempfile.mkdtemp(prefix="corebench-env-probe-", dir=scratch_root))
    try:
        for probe_attempt in range(2):
            name = f"aletheia-corebench-env-{uuid.uuid4().hex[:16]}"
            command = hardened_docker_command(
                root,
                image_id=image_id,
                container_name=name,
                command=["python", "-c", probe],
                memory_mb=256,
                cpus=1,
                cpu_seconds=20,
                include_aletheia_pythonpath=False,
            )
            result = run_hardened_container(
                command, container_name=name, timeout_s=30, image_id=image_id
            )
            if result.ok:
                return {
                    str(key): str(value)
                    for key, value in json.loads(result.output.splitlines()[-1]).items()
                }
            # This is evaluator-owned setup, not a scientific candidate attempt. A stopped
            # container whose Docker client did not close is explicitly classified as infra and
            # may be retried once; running-container timeouts and all authored failures are not.
            if (
                probe_attempt == 0
                and not result.timed_out
                and result.error == "Docker client did not exit after its container stopped"
            ):
                continue
            raise RuntimeError(f"could not inspect CORE-Bench image environment: {result.output}")
        raise RuntimeError("could not inspect CORE-Bench image environment")
    finally:
        shutil.rmtree(root, ignore_errors=True)
