"""Freeze the two-container DiscoveryWorld public-validation mini-suite."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from aletheia.evals.adapters.discoveryworld import (
    DEFAULT_DISCOVERYWORLD_SPECS,
    DiscoveryWorldAdapter,
    DiscoveryWorldInstanceSpec,
    DiscoveryWorldScorer,
    DiscoveryWorldSourceManifest,
    DockerDiscoveryWorldHarness,
)
from aletheia.evals.schemas import ResourceBudget


def _atomic_json(path: Path, payload: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _instance(value: str) -> DiscoveryWorldInstanceSpec:
    instance_id, separator, raw_seed = value.partition("=")
    if not separator or not instance_id or not raw_seed:
        raise argparse.ArgumentTypeError("instances must use INSTANCE_ID=WORLD_SEED")
    try:
        seed = int(raw_seed)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("DiscoveryWorld seed must be an integer") from exc
    try:
        return DiscoveryWorldInstanceSpec(instance_id=instance_id, world_seed=seed)
    except Exception as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    from aletheia.config import get_settings

    settings = get_settings()
    parser = argparse.ArgumentParser(
        description=(
            "Verify immutable candidate/environment images, extract official hidden rules inside "
            "the trusted image, and emit a frozen DiscoveryWorld validation suite."
        )
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--candidate-image", default=settings.discoveryworld_candidate_docker_image)
    parser.add_argument("--environment-image", default=settings.discoveryworld_docker_image)
    parser.add_argument(
        "--instance",
        action="append",
        type=_instance,
        dest="instances",
        help="opaque public instance ID and official evaluator-only world seed",
    )
    parser.add_argument("--reproduction-runs", type=int, default=2)
    parser.add_argument("--max-world-actions", type=int, default=80)
    parser.add_argument("--candidate-wall-time-s", type=int, default=120)
    parser.add_argument("--candidate-cpu-seconds", type=int, default=90)
    parser.add_argument("--candidate-memory-mb", type=int, default=512)
    parser.add_argument("--environment-wall-time-s", type=int, default=150)
    parser.add_argument("--environment-cpu-seconds", type=int, default=120)
    parser.add_argument("--environment-memory-mb", type=int, default=2048)
    parser.add_argument("--wall-time-s", type=int, default=600)
    parser.add_argument("--cpu-seconds", type=int, default=300)
    parser.add_argument("--memory-mb", type=int, default=1024)
    parser.add_argument("--token-cap", type=int)
    parser.add_argument("--usd-cap", type=float)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    output_root = args.output_root.expanduser().resolve(strict=False)
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.source_manifest:
        source = DiscoveryWorldSourceManifest.model_validate_json(
            args.source_manifest.expanduser().resolve(strict=True).read_bytes()
        )
    else:
        source = DiscoveryWorldSourceManifest.official_public_validation()
    adapter = DiscoveryWorldAdapter(source)
    specs = tuple(
        args.instances
        or (
            DiscoveryWorldInstanceSpec(instance_id=instance_id, world_seed=seed)
            for instance_id, seed in DEFAULT_DISCOVERYWORLD_SPECS
        )
    )
    subset = adapter.select_subset(specs)
    harness = DockerDiscoveryWorldHarness.from_image_refs(
        candidate_image_ref=args.candidate_image,
        environment_image_ref=args.environment_image,
        source_manifest_sha256=source.manifest_sha256,
        scratch_root=output_root / "scratch",
        reproduction_runs=args.reproduction_runs,
        max_world_actions=args.max_world_actions,
        candidate_wall_time_s=args.candidate_wall_time_s,
        candidate_cpu_seconds=args.candidate_cpu_seconds,
        candidate_memory_mb=args.candidate_memory_mb,
        environment_wall_time_s=args.environment_wall_time_s,
        environment_cpu_seconds=args.environment_cpu_seconds,
        environment_memory_mb=args.environment_memory_mb,
        action_wait_s=max(args.candidate_wall_time_s + 5, 10),
    )
    receipts = [harness.freeze_instance(spec=spec) for spec in subset.instance_specs]
    scorer = DiscoveryWorldScorer(harness=harness, source_manifest_sha256=source.manifest_sha256)
    budget = ResourceBudget(
        wall_time_s=args.wall_time_s,
        cpu_seconds=args.cpu_seconds,
        memory_mb=args.memory_mb,
        token_cap=args.token_cap,
        usd_cap=args.usd_cap,
    )
    tasks = [
        adapter.build_task(receipt=receipt, scorer=scorer, resource_budget=budget)
        for receipt in receipts
    ]
    suite = adapter.build_suite(tasks=tasks, subset_manifest=subset, scorer=scorer)
    hidden_paths = [
        adapter.stage_hidden_asset(evaluator_root=output_root, task=task, receipt=receipt)
        for task, receipt in zip(tasks, receipts, strict=True)
    ]
    bundle = {
        "schema_version": 1,
        "source_manifest": source.model_dump(mode="json"),
        "subset_manifest": subset.model_dump(mode="json"),
        "harness_manifest": harness.manifest.model_dump(mode="json"),
        "scorer_sha256": scorer.scorer_sha256,
        "suite": suite.model_dump(mode="json"),
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "public_tasks": [task.public_view().model_dump(mode="json") for task in tasks],
        "hidden_asset_paths": [str(path.relative_to(output_root)) for path in hidden_paths],
        "hidden_asset_sha256s": [receipt.hidden_sha256 for receipt in receipts],
        "hidden_receipts_embedded_in_bundle": False,
        "candidate_receives_world_seed_rule_scorecard_or_scorer": False,
        "official_source_or_art_assets_vendored_into_suite": False,
        "intended_use": "public validation only; not the private Frontier Scientist Gate",
    }
    destination = output_root / "discoveryworld_suite.v1.json"
    _atomic_json(destination, bundle)
    print(destination)


if __name__ == "__main__":
    main()
