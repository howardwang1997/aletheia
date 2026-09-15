"""Read-compute-return adapter for the seeded cuprate diagnostic.

The service holds exactly one piece of deployment state: the pinned,
read-only staged path of the registered dataset card's CSV bytes.  Every
request verifies the staged bytes against the registered content identity
before any row reaches the pipeline, then restricts the rows to the
protocol's bound batch and runs both preregistered discriminators.

No database, no admission, no signing authority: the method reads one
file, computes, and returns a typed result.
"""

from __future__ import annotations

from pathlib import Path

from aletheia.execution.cuprate.card_rows import restrict_to_bound_batch
from aletheia.execution.cuprate.diagnostics import run_cuprate_diagnostic
from aletheia.research_controller.external_rpc import CuprateDiagnosticResult
from aletheia.research_controller.external_rpc_server import CuprateDiagnosticRPCPayload


class CuprateDiagnosticService:
    """One staged dataset, one seeded diagnostic, no other authority."""

    def __init__(self, *, dataset_csv_path: Path) -> None:
        path = Path(dataset_csv_path)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("cuprate diagnostic dataset path must be an absolute regular path")
        self._dataset_csv_path = path

    @property
    def dataset_csv_path(self) -> Path:
        return self._dataset_csv_path

    def run_cuprate_diagnostic(
        self, payload: CuprateDiagnosticRPCPayload
    ) -> CuprateDiagnosticResult:
        """Verify the staged bytes, restrict to the bound batch, run both arms."""

        if type(payload) is not CuprateDiagnosticRPCPayload:
            raise TypeError("cuprate diagnostic RPC handler received another payload type")
        csv_bytes = self._dataset_csv_path.read_bytes()
        batch = restrict_to_bound_batch(
            csv_bytes=csv_bytes,
            expected_content_sha256=payload.expected_content_sha256,
            composition_column=payload.composition_column,
            target_column=payload.target_column,
            bound_batch_group_ids=payload.bound_batch_group_ids,
        )
        result = run_cuprate_diagnostic(
            formulas=batch.formulas,
            targets=batch.targets,
            doping_optimum=payload.doping_optimum,
        )
        return CuprateDiagnosticResult(
            dataset_content_sha256=payload.expected_content_sha256,
            doping_optimum=payload.doping_optimum,
            analyzed_rows=len(batch.formulas),
            dropped_off_batch_rows=batch.dropped_off_batch,
            d1_matched_control=result["d1_matched_control"],
            d2_doping_stratification=result["d2_doping_stratification"],
        )
