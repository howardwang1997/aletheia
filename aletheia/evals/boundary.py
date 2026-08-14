"""Deterministic policy for separating research workspaces from hidden evaluator assets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class EvaluationBoundaryError(RuntimeError):
    pass


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


@dataclass(frozen=True)
class EvaluationBoundary:
    """Paths and capabilities exposed on each side of the evaluation plane."""

    research_workspace: Path
    submission_inbox: Path
    evaluator_workspace: Path
    hidden_assets_root: Path
    public_inputs_root: Path | None = None
    public_assets_root: Path | None = None

    def __post_init__(self) -> None:
        roots = [
            _resolved(self.research_workspace),
            _resolved(self.submission_inbox),
            _resolved(self.evaluator_workspace),
            _resolved(self.hidden_assets_root),
        ]
        if self.public_inputs_root is not None:
            roots.append(_resolved(self.public_inputs_root))
        if self.public_assets_root is not None:
            roots.append(_resolved(self.public_assets_root))
        for index, root in enumerate(roots):
            for other in roots[index + 1 :]:
                if root == other or root in other.parents or other in root.parents:
                    raise EvaluationBoundaryError(
                        f"evaluation roots must be disjoint, got overlapping {root} and {other}"
                    )

    def assert_research_write(self, path: Path) -> Path:
        target = _resolved(path)
        allowed = (_resolved(self.research_workspace), _resolved(self.submission_inbox))
        if not any(target == root or root in target.parents for root in allowed):
            raise EvaluationBoundaryError(f"research process may not write outside its roots: {target}")
        if self._is_evaluator_path(target):
            raise EvaluationBoundaryError(f"research process may not access evaluator assets: {target}")
        return target

    def assert_research_read(self, path: Path) -> Path:
        target = _resolved(path)
        if self._is_evaluator_path(target):
            raise EvaluationBoundaryError(f"research process may not read evaluator assets: {target}")
        roots = [_resolved(self.research_workspace)]
        if self.public_inputs_root is not None:
            roots.append(_resolved(self.public_inputs_root))
        if not any(target == root or root in target.parents for root in roots):
            raise EvaluationBoundaryError(
                f"research process may read only its workspace and public inputs: {target}"
            )
        return target

    def assert_submission_read(self, path: Path) -> Path:
        target = _resolved(path)
        inbox = _resolved(self.submission_inbox)
        if not (target == inbox or inbox in target.parents):
            raise EvaluationBoundaryError(f"evaluator submission read escaped inbox: {target}")
        return target

    def assert_public_asset_read(self, path: Path) -> Path:
        if self.public_assets_root is None:
            raise EvaluationBoundaryError("no evaluator public-asset root is configured")
        target = _resolved(path)
        root = _resolved(self.public_assets_root)
        if not (target == root or root in target.parents):
            raise EvaluationBoundaryError(f"evaluator public-asset read escaped its root: {target}")
        return target

    def assert_evaluator_write(self, path: Path) -> Path:
        target = _resolved(path)
        root = _resolved(self.evaluator_workspace)
        if not (target == root or root in target.parents):
            raise EvaluationBoundaryError(f"evaluator may write only evaluator workspace: {target}")
        return target

    def _is_evaluator_path(self, path: Path) -> bool:
        for root in (_resolved(self.evaluator_workspace), _resolved(self.hidden_assets_root)):
            if path == root or root in path.parents:
                return True
        return False


RESEARCH_ALLOWED_CAPABILITIES = frozenset({"read_public_task", "write_submission"})
EVALUATOR_ALLOWED_CAPABILITIES = frozenset(
    {"read_submission", "read_hidden_asset", "execute_scorer", "write_score_receipt"}
)


def validate_capabilities(*, plane: str, requested: set[str]) -> None:
    allowed = (
        RESEARCH_ALLOWED_CAPABILITIES
        if plane == "research"
        else EVALUATOR_ALLOWED_CAPABILITIES
        if plane == "evaluator"
        else frozenset()
    )
    denied = requested - allowed
    if denied:
        raise EvaluationBoundaryError(
            f"{plane!r} plane requested forbidden capabilities: {sorted(denied)}"
        )
