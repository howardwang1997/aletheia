"""Compute abstraction. The orchestrator never runs raw training in its own
context — it submits a job and polls. Phase 1 ships a local subprocess backend;
docker/slurm/k8s slot behind the same ABC later, and the same seam generalizes to
a physical ``ExperimentBackend`` (self-driving lab) in the long-term roadmap.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class JobSpec:
    run_id: str
    domain: str
    design: dict[str, Any]
    data_spec: dict[str, Any]
    experiment_id: str | None = None
    kind: str = "train_eval"
    dry_run: bool = False  # compute-dry-run: synthesize metrics, spawn nothing


@dataclass
class JobStatus:
    job_id: str
    status: str  # queued | running | done | failed
    metrics: dict[str, float] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def finished(self) -> bool:
        return self.status in ("done", "failed")


class ComputeBackend(ABC):
    name: str = "base"

    @abstractmethod
    def submit(self, spec: JobSpec) -> str:
        """Enqueue/launch a job. Returns a job_id (the ledger ComputeJob id)."""

    @abstractmethod
    def status(self, job_id: str) -> JobStatus:
        """Poll a job; finalizes (persists metrics/artifacts) on first completion."""

    @abstractmethod
    def logs(self, job_id: str) -> str:
        """Combined stdout/stderr captured for the job."""

    @abstractmethod
    def cancel(self, job_id: str) -> None:
        ...

    @abstractmethod
    def capacity(self) -> dict[str, Any]:
        """Coarse capacity/utilization snapshot for the dashboard."""
