"""Deterministic F9-v2 continuation assessment with fresh artifact custody.

The assessor recognizes only one closed prediction identity: an exact outcome bin from the
admission policy, bound to the same observable, measurement protocol, and outcome space as the
admitted observation.  An opaque or ambiguous prediction is indeterminate rather than silently
treated as evidence against a hypothesis.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from aletheia.protocols.world_models import HypothesisLifecycle, PredictionVersionV2
from aletheia.research_controller.continuation import (
    HypothesisPredictionAssessment,
    PredictionFit,
    exact_outcome_bin_prediction_sha256,
)
from aletheia.research_controller.continuation_step import (
    AuthorizedContinuationAssessmentContext,
    ContinuationAssessmentPolicyPin,
    ContinuationAssessmentStepError,
    PreparedContinuationAssessment,
)
from aletheia.research_controller.contracts import ControllerModel
from aletheia.research_kernel.schemas import canonical_json_bytes, canonical_sha256

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_OUTCOME_BIN_PATTERN = r"^[a-z][a-z0-9_.:/-]{1,127}$"
_MAX_ARTIFACT_BYTES = 256 * 1024

EXACT_OUTCOME_BIN_FIT_RULE_SHA256 = canonical_sha256(
    {
        "schema_name": "aletheia.exact_outcome_bin_fit_rule",
        "schema_version": 1,
        "recognized_prediction": "exact_admissible_outcome_bin_identity",
        "same_bin": PredictionFit.IN_SUPPORT,
        "different_recognized_bin": PredictionFit.OUT_OF_SUPPORT,
        "unknown_or_ambiguous_prediction": PredictionFit.INDETERMINATE,
        "missing_prediction": "omit_assessment_for_typed_redesign",
    }
)


class ContinuationAssessmentArtifactError(ContinuationAssessmentStepError):
    """An exact fit artifact or its filesystem custody failed closed."""


class ExactOutcomeFitAssessmentArtifact(ControllerModel):
    """Reconstructable evidence for one exact hypothesis/prediction fit decision."""

    schema_name: Literal["aletheia.exact_outcome_fit_assessment_artifact"] = (
        "aletheia.exact_outcome_fit_assessment_artifact"
    )
    schema_version: Literal[1] = 1
    context_sha256: str = Field(pattern=_SHA256_PATTERN)
    hypothesis_sha256: str = Field(pattern=_SHA256_PATTERN)
    selected_prediction_sha256: str = Field(pattern=_SHA256_PATTERN)
    exact_context_prediction_sha256s: tuple[str, ...] = Field(min_length=1, max_length=1024)
    predicted_outcome_sha256: str = Field(pattern=_SHA256_PATTERN)
    resolved_predicted_outcome_bin_id: str | None = Field(
        default=None,
        pattern=_OUTCOME_BIN_PATTERN,
    )
    observed_outcome_bin_id: str = Field(pattern=_OUTCOME_BIN_PATTERN)
    admissible_outcome_bin_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    prediction_fit: PredictionFit
    fit_rule_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _artifact_is_canonical(self) -> "ExactOutcomeFitAssessmentArtifact":
        if (
            self.exact_context_prediction_sha256s
            != tuple(sorted(set(self.exact_context_prediction_sha256s)))
            or self.selected_prediction_sha256 not in self.exact_context_prediction_sha256s
            or self.admissible_outcome_bin_ids
            != tuple(sorted(set(self.admissible_outcome_bin_ids)))
            or self.observed_outcome_bin_id not in self.admissible_outcome_bin_ids
            or self.fit_rule_sha256 != EXACT_OUTCOME_BIN_FIT_RULE_SHA256
        ):
            raise ValueError("exact outcome-fit artifact is not canonical")
        ambiguous = len(self.exact_context_prediction_sha256s) != 1
        unresolved = self.resolved_predicted_outcome_bin_id is None
        if ambiguous and not unresolved:
            raise ValueError("ambiguous prediction cannot resolve an outcome bin")
        if (
            self.resolved_predicted_outcome_bin_id is not None
            and self.resolved_predicted_outcome_bin_id not in self.admissible_outcome_bin_ids
        ):
            raise ValueError("resolved prediction is outside the admissible outcome bins")
        if ambiguous or unresolved:
            if self.prediction_fit is not PredictionFit.INDETERMINATE:
                raise ValueError("ambiguous or opaque prediction must remain indeterminate")
        else:
            expected = (
                PredictionFit.IN_SUPPORT
                if self.resolved_predicted_outcome_bin_id == self.observed_outcome_bin_id
                else PredictionFit.OUT_OF_SUPPORT
            )
            if self.prediction_fit is not expected:
                raise ValueError("recognized outcome-bin prediction has another fit")
        return self

    @property
    def artifact_sha256(self) -> str:
        return canonical_sha256(self)


def _exact_context_predictions(
    context: AuthorizedContinuationAssessmentContext,
    *,
    hypothesis_sha256: str,
) -> tuple[PredictionVersionV2, ...]:
    observation = context.observation
    return tuple(
        sorted(
            (
                prediction
                for prediction in context.world_model.predictions
                if prediction.hypothesis_sha256 == hypothesis_sha256
                and (
                    prediction.observable_spec_sha256,
                    prediction.measurement_protocol_sha256,
                    prediction.outcome_space_sha256,
                )
                == (
                    observation.observable_spec_sha256,
                    observation.measurement_protocol_sha256,
                    observation.outcome_space_sha256,
                )
            ),
            key=lambda item: item.prediction_sha256,
        )
    )


def build_exact_outcome_fit_assessment_artifact(
    context: AuthorizedContinuationAssessmentContext,
    *,
    hypothesis_sha256: str,
) -> ExactOutcomeFitAssessmentArtifact | None:
    """Build the only supported fit artifact, or omit a missing exact-context prediction."""

    predictions = _exact_context_predictions(context, hypothesis_sha256=hypothesis_sha256)
    if not predictions:
        return None
    selected = predictions[0]
    observation = context.observation
    resolved_bin: str | None = None
    if len(predictions) == 1:
        matches = tuple(
            outcome_bin_id
            for outcome_bin_id in observation.admissible_outcome_bin_ids
            if exact_outcome_bin_prediction_sha256(
                observable_spec_sha256=observation.observable_spec_sha256,
                measurement_protocol_sha256=observation.measurement_protocol_sha256,
                outcome_space_sha256=observation.outcome_space_sha256,
                outcome_bin_id=outcome_bin_id,
            )
            == selected.predicted_outcome_sha256
        )
        if len(matches) == 1:
            resolved_bin = matches[0]
    if resolved_bin is None:
        fit = PredictionFit.INDETERMINATE
    elif resolved_bin == observation.observed_outcome_bin_id:
        fit = PredictionFit.IN_SUPPORT
    else:
        fit = PredictionFit.OUT_OF_SUPPORT
    return ExactOutcomeFitAssessmentArtifact(
        context_sha256=context.context_sha256,
        hypothesis_sha256=hypothesis_sha256,
        selected_prediction_sha256=selected.prediction_sha256,
        exact_context_prediction_sha256s=tuple(
            prediction.prediction_sha256 for prediction in predictions
        ),
        predicted_outcome_sha256=selected.predicted_outcome_sha256,
        resolved_predicted_outcome_bin_id=resolved_bin,
        observed_outcome_bin_id=observation.observed_outcome_bin_id,
        admissible_outcome_bin_ids=observation.admissible_outcome_bin_ids,
        prediction_fit=fit,
        fit_rule_sha256=EXACT_OUTCOME_BIN_FIT_RULE_SHA256,
    )


class WriteOnceContinuationAssessmentArtifactArchive:
    """Service-owned CAS that reopens and rehashes every referenced fit artifact."""

    def __init__(
        self,
        root: Path,
        *,
        owner_uid: int,
        owner_gid: int,
        device_id: int,
        inode: int,
        directory_mode: int = 0o700,
    ) -> None:
        candidate = Path(root)
        if not candidate.is_absolute() or str(candidate) != os.path.normpath(candidate):
            raise ValueError("continuation artifact root must be canonical and absolute")
        self.root = candidate
        self.owner_uid = owner_uid
        self.owner_gid = owner_gid
        self.device_id = device_id
        self.inode = inode
        self.directory_mode = directory_mode
        self._verify_root()

    def _verify_root(self) -> None:
        try:
            resolved = self.root.resolve(strict=True)
            metadata = self.root.lstat()
        except OSError as exc:
            raise ContinuationAssessmentArtifactError(
                "continuation artifact root is unavailable"
            ) from exc
        if (
            resolved != self.root
            or self.root.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self.owner_uid
            or metadata.st_gid != self.owner_gid
            or metadata.st_dev != self.device_id
            or metadata.st_ino != self.inode
            or stat.S_IMODE(metadata.st_mode) != self.directory_mode
            or self.directory_mode != 0o700
        ):
            raise ContinuationAssessmentArtifactError(
                "continuation artifact root differs from its custody pin"
            )

    def _open_parent(self, digest: str, *, create: bool) -> int:
        self._verify_root()
        root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.root, root_flags)
        try:
            for component in ("sha256", digest[:2]):
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                        os.fsync(descriptor)
                    except FileExistsError:
                        pass
                child = os.open(component, root_flags, dir_fd=descriptor)
                metadata = os.fstat(child)
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != self.owner_uid
                    or metadata.st_gid != self.owner_gid
                    or metadata.st_dev != self.device_id
                    or stat.S_IMODE(metadata.st_mode) != 0o700
                ):
                    os.close(child)
                    raise ContinuationAssessmentArtifactError(
                        "continuation artifact parent chain differs from its custody"
                    )
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def put_once(self, artifact: ExactOutcomeFitAssessmentArtifact) -> str:
        artifact = ExactOutcomeFitAssessmentArtifact.model_validate(
            artifact.model_dump(mode="python")
        )
        payload = canonical_json_bytes(artifact)
        if not payload or len(payload) > _MAX_ARTIFACT_BYTES:
            raise ContinuationAssessmentArtifactError(
                "continuation assessment artifact exceeds its byte bound"
            )
        digest = hashlib.sha256(payload).hexdigest()
        if digest != artifact.artifact_sha256:
            raise ContinuationAssessmentArtifactError(
                "continuation assessment artifact identity is inconsistent"
            )
        parent = self._open_parent(digest, create=True)
        staging = f".{digest}.{secrets.token_hex(16)}.tmp"
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(staging, flags, 0o400, dir_fd=parent)
            complete = False
            try:
                offset = 0
                while offset < len(payload):
                    written = os.write(descriptor, payload[offset:])
                    if written <= 0:
                        raise ContinuationAssessmentArtifactError(
                            "continuation artifact write made no progress"
                        )
                    offset += written
                os.fchmod(descriptor, 0o400)
                os.fsync(descriptor)
                complete = True
            finally:
                os.close(descriptor)
                if not complete:
                    try:
                        os.unlink(staging, dir_fd=parent)
                    except FileNotFoundError:
                        pass
            try:
                os.link(
                    staging,
                    digest,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
            except FileExistsError:
                pass
            finally:
                os.unlink(staging, dir_fd=parent)
            os.fsync(parent)
        except OSError as exc:
            raise ContinuationAssessmentArtifactError(
                "continuation artifact publication failed closed"
            ) from exc
        finally:
            os.close(parent)
        if self.load(digest) != artifact:
            raise ContinuationAssessmentArtifactError(
                "continuation artifact publication recovered another value"
            )
        return digest

    def load(self, artifact_sha256: str) -> ExactOutcomeFitAssessmentArtifact:
        if re.fullmatch(_SHA256_PATTERN, artifact_sha256) is None:
            raise ContinuationAssessmentArtifactError(
                "continuation artifact identity is not SHA-256"
            )
        parent = self._open_parent(artifact_sha256, create=False)
        try:
            descriptor = os.open(
                artifact_sha256,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            try:
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_uid != self.owner_uid
                    or before.st_gid != self.owner_gid
                    or before.st_dev != self.device_id
                    or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) != 0o400
                    or not 0 < before.st_size <= _MAX_ARTIFACT_BYTES
                ):
                    raise ContinuationAssessmentArtifactError(
                        "continuation artifact is not immutable service-owned data"
                    )
                chunks: list[bytes] = []
                remaining = before.st_size
                while remaining:
                    chunk = os.read(descriptor, min(65_536, remaining))
                    if not chunk:
                        raise ContinuationAssessmentArtifactError(
                            "continuation artifact ended unexpectedly"
                        )
                    chunks.append(chunk)
                    remaining -= len(chunk)
                after = os.fstat(descriptor)
                if os.read(descriptor, 1) or (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ContinuationAssessmentArtifactError(
                        "continuation artifact changed during read"
                    )
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ContinuationAssessmentArtifactError(
                "continuation artifact is missing or unsafe"
            ) from exc
        finally:
            os.close(parent)
        payload = b"".join(chunks)
        if hashlib.sha256(payload).hexdigest() != artifact_sha256:
            raise ContinuationAssessmentArtifactError("continuation artifact bytes changed")
        try:
            artifact = ExactOutcomeFitAssessmentArtifact.model_validate_json(payload)
        except ValueError as exc:
            raise ContinuationAssessmentArtifactError(
                "continuation artifact payload is invalid"
            ) from exc
        if canonical_json_bytes(artifact) != payload or artifact.artifact_sha256 != artifact_sha256:
            raise ContinuationAssessmentArtifactError("continuation artifact is not canonical")
        return artifact

    def verify_assessment_artifacts(
        self,
        *,
        context: AuthorizedContinuationAssessmentContext,
        assessments: tuple[HypothesisPredictionAssessment, ...],
    ) -> None:
        expected = tuple(
            artifact
            for hypothesis in context.world_model.hypotheses
            if hypothesis.lifecycle is HypothesisLifecycle.ACTIVE
            for artifact in (
                build_exact_outcome_fit_assessment_artifact(
                    context,
                    hypothesis_sha256=hypothesis.hypothesis_sha256,
                ),
            )
            if artifact is not None
        )
        expected = tuple(
            sorted(
                expected,
                key=lambda item: (
                    item.hypothesis_sha256,
                    item.selected_prediction_sha256,
                    item.artifact_sha256,
                ),
            )
        )
        if len(expected) != len(assessments):
            raise ContinuationAssessmentArtifactError(
                "continuation assessment artifact set is incomplete"
            )
        for assessment, expected_artifact in zip(assessments, expected, strict=True):
            if (
                assessment.hypothesis_sha256 != expected_artifact.hypothesis_sha256
                or assessment.prediction_sha256 != expected_artifact.selected_prediction_sha256
                or assessment.prediction_fit is not expected_artifact.prediction_fit
                or assessment.fit_rule_sha256 != expected_artifact.fit_rule_sha256
                or assessment.assessment_artifact_sha256 != expected_artifact.artifact_sha256
                or self.load(assessment.assessment_artifact_sha256) != expected_artifact
            ):
                raise ContinuationAssessmentArtifactError(
                    "continuation assessment differs from fresh artifact custody"
                )


class ExactOutcomeBinContinuationAssessor:
    """Powerless provider for the closed exact-outcome-bin fit rule."""

    def __init__(
        self,
        *,
        policy: ContinuationAssessmentPolicyPin,
        principal_id: str,
        implementation_sha256: str,
        artifacts: WriteOnceContinuationAssessmentArtifactArchive,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        policy = ContinuationAssessmentPolicyPin.model_validate(policy.model_dump(mode="python"))
        if (
            implementation_sha256 != policy.assessment_implementation_sha256
            or principal_id not in policy.allowed_assessor_principal_ids
            or EXACT_OUTCOME_BIN_FIT_RULE_SHA256 not in policy.allowed_fit_rule_sha256s
        ):
            raise ValueError("exact outcome-bin assessor differs from its policy")
        self._policy = policy
        self._principal_id = principal_id
        self._implementation_sha256 = implementation_sha256
        self._artifacts = artifacts
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def assess_continuation(
        self,
        context: AuthorizedContinuationAssessmentContext,
    ) -> PreparedContinuationAssessment:
        context = AuthorizedContinuationAssessmentContext.model_validate(
            context.model_dump(mode="python")
        )
        if context.assessment_policy != self._policy:
            raise ContinuationAssessmentArtifactError(
                "continuation assessor context changed its deployment policy"
            )
        artifacts = tuple(
            artifact
            for hypothesis in context.world_model.hypotheses
            if hypothesis.lifecycle is HypothesisLifecycle.ACTIVE
            for artifact in (
                build_exact_outcome_fit_assessment_artifact(
                    context,
                    hypothesis_sha256=hypothesis.hypothesis_sha256,
                ),
            )
            if artifact is not None
        )
        assessments = tuple(
            sorted(
                (
                    HypothesisPredictionAssessment(
                        hypothesis_sha256=artifact.hypothesis_sha256,
                        prediction_sha256=artifact.selected_prediction_sha256,
                        prediction_fit=artifact.prediction_fit,
                        fit_rule_sha256=artifact.fit_rule_sha256,
                        assessment_artifact_sha256=self._artifacts.put_once(artifact),
                    )
                    for artifact in artifacts
                ),
                key=lambda item: (
                    item.hypothesis_sha256,
                    item.prediction_sha256,
                    item.assessment_sha256,
                ),
            )
        )
        assessed_at = self._clock()
        if assessed_at.tzinfo is None or assessed_at.utcoffset() is None:
            raise ContinuationAssessmentArtifactError("continuation assessor clock must be aware")
        return PreparedContinuationAssessment(
            context_sha256=context.context_sha256,
            assessments=assessments,
            assessment_implementation_sha256=self._implementation_sha256,
            assessed_by_principal_id=self._principal_id,
            assessed_at=assessed_at,
        )


__all__ = [
    "ContinuationAssessmentArtifactError",
    "EXACT_OUTCOME_BIN_FIT_RULE_SHA256",
    "ExactOutcomeBinContinuationAssessor",
    "ExactOutcomeFitAssessmentArtifact",
    "WriteOnceContinuationAssessmentArtifactArchive",
    "build_exact_outcome_fit_assessment_artifact",
]
