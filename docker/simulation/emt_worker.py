"""Network-free ASE/EMT equation-of-state worker used by the F10-S5 image."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import ase
import numpy as np
import scipy
from ase import Atoms
from ase.calculators.emt import EMT
from ase.eos import EquationOfState


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_bytes(value))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _finite_matrix(value: Any, *, rows: int, columns: int, name: str) -> np.ndarray:
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (rows, columns) or not np.isfinite(matrix).all():
        raise ValueError(f"{name} must be a finite {rows}x{columns} matrix")
    return matrix


def _load_job(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    job = json.loads(raw)
    if job.get("schema_name") != "aletheia.ase_emt_eos_job" or job.get("schema_version") != 1:
        raise ValueError("unsupported simulation job schema")
    if set(job) != {
        "schema_name",
        "schema_version",
        "job_id",
        "calculator",
        "structure",
        "scan",
    }:
        raise ValueError("simulation job has unknown or missing top-level fields")
    return job, raw


def _execute(job: dict[str, Any], raw: bytes, output_directory: Path) -> dict[str, Any]:
    calculator = job["calculator"]
    if set(calculator) != {"name", "asap_cutoff"} or calculator["name"] != "ase.emt":
        raise ValueError("only the frozen ASE EMT calculator is supported")
    if not isinstance(calculator["asap_cutoff"], bool):
        raise ValueError("asap_cutoff must be boolean")

    structure = job["structure"]
    if set(structure) != {"symbols", "positions_angstrom", "cell_angstrom", "pbc"}:
        raise ValueError("simulation structure has unknown or missing fields")
    symbols = structure["symbols"]
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 256:
        raise ValueError("simulation requires between 1 and 256 atomic sites")
    positions = _finite_matrix(
        structure["positions_angstrom"], rows=len(symbols), columns=3, name="positions"
    )
    cell = _finite_matrix(structure["cell_angstrom"], rows=3, columns=3, name="cell")
    if abs(float(np.linalg.det(cell))) <= 1e-6:
        raise ValueError("simulation cell must have nonzero volume")
    pbc = structure["pbc"]
    if pbc != [True, True, True]:
        raise ValueError("equation-of-state simulation requires three-dimensional periodicity")

    scan = job["scan"]
    if set(scan) != {"eos_model", "points", "volume_strain_fraction"}:
        raise ValueError("simulation scan has unknown or missing fields")
    if scan["eos_model"] != "sj":
        raise ValueError("only the frozen stabilized-jellium EOS fit is supported")
    points = scan["points"]
    strain = scan["volume_strain_fraction"]
    if not isinstance(points, int) or not 5 <= points <= 31 or points % 2 != 1:
        raise ValueError("EOS point count must be an odd integer from 5 through 31")
    if not isinstance(strain, (int, float)) or not 0.005 <= float(strain) <= 0.20:
        raise ValueError("EOS volume strain fraction is outside the safe policy")

    atoms = Atoms(symbols=symbols, positions=positions, cell=cell, pbc=pbc)
    initial_cell = atoms.cell.copy()
    observations: list[dict[str, Any]] = []
    for index, factor in enumerate(
        np.linspace(1.0 - float(strain), 1.0 + float(strain), points) ** (1.0 / 3.0)
    ):
        sample = atoms.copy()
        sample.set_cell(float(factor) * initial_cell, scale_atoms=True)
        sample.calc = EMT(asap_cutoff=calculator["asap_cutoff"])
        energy = float(sample.get_potential_energy())
        forces = np.asarray(sample.get_forces(), dtype=np.float64)
        stress = np.asarray(sample.get_stress(voigt=False), dtype=np.float64)
        values = (energy, float(sample.get_volume()), *forces.ravel(), *stress.ravel())
        if any(not math.isfinite(float(value)) for value in values):
            raise ValueError("calculator produced a nonfinite value")
        observations.append(
            {
                "index": index,
                "volume_angstrom3": float(sample.get_volume()),
                "energy_eV": energy,
                "maximum_force_eV_per_angstrom": float(
                    np.linalg.norm(forces, axis=1).max(initial=0.0)
                ),
                "maximum_absolute_stress_eV_per_angstrom3": float(np.abs(stress).max()),
            }
        )
        _atomic_json(
            output_directory / "checkpoint.json",
            {
                "schema_name": "aletheia.ase_emt_eos_checkpoint",
                "schema_version": 1,
                "job_id": job["job_id"],
                "input_sha256": hashlib.sha256(raw).hexdigest(),
                "completed_evaluations": len(observations),
                "observations": observations,
            },
        )

    volumes = [item["volume_angstrom3"] for item in observations]
    energies = [item["energy_eV"] for item in observations]
    equation = EquationOfState(volumes, energies, eos=scan["eos_model"])
    equilibrium_volume, minimum_energy, bulk_modulus = equation.fit()
    fitted_energies = np.asarray(equation.fit0(np.asarray(volumes) ** (-1.0 / 3.0)))
    residuals = np.asarray(energies, dtype=np.float64) - fitted_energies
    result: dict[str, Any] = {
        "schema_name": "aletheia.ase_emt_eos_result",
        "schema_version": 1,
        "job_id": job["job_id"],
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "runtime_versions": {
            "ase": ase.__version__,
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
        "calculator": calculator,
        "scan": scan,
        "site_count": len(atoms),
        "evaluations": len(observations),
        "observations": observations,
        "equilibrium_volume_angstrom3": float(equilibrium_volume),
        "equilibrium_volume_per_atom_angstrom3": float(equilibrium_volume / len(atoms)),
        "minimum_energy_eV": float(minimum_energy),
        "bulk_modulus_eV_per_angstrom3": float(bulk_modulus),
        "fit_rmse_eV": float(np.sqrt(np.mean(residuals**2))),
        "fit_maximum_absolute_residual_eV": float(np.abs(residuals).max()),
        "fit_inside_scanned_volume_range": bool(min(volumes) <= equilibrium_volume <= max(volumes)),
        "sample_minimum_is_interior": bool(0 < int(np.argmin(energies)) < len(energies) - 1),
    }
    if any(
        not math.isfinite(float(result[name]))
        for name in (
            "equilibrium_volume_angstrom3",
            "equilibrium_volume_per_atom_angstrom3",
            "minimum_energy_eV",
            "bulk_modulus_eV_per_angstrom3",
            "fit_rmse_eV",
            "fit_maximum_absolute_residual_eV",
        )
    ):
        raise ValueError("EOS fit produced a nonfinite value")
    result["payload_sha256"] = _sha256(result)
    return result


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: emt_worker.py INPUT_JSON OUTPUT_DIRECTORY", file=sys.stderr)
        return 64
    input_path = Path(sys.argv[1]).resolve(strict=True)
    output_directory = Path(sys.argv[2]).resolve(strict=True)
    try:
        job, raw = _load_job(input_path)
        result = _execute(job, raw, output_directory)
        _atomic_json(output_directory / "result.json", result)
        print(json.dumps({"status": "completed", "payload_sha256": result["payload_sha256"]}))
        return 0
    except Exception as error:
        failure = {
            "schema_name": "aletheia.ase_emt_failure",
            "schema_version": 1,
            "error_type": type(error).__name__,
            "message": str(error)[:1000],
        }
        try:
            _atomic_json(output_directory / "failure.json", failure)
        except Exception:
            pass
        print(json.dumps(failure), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
