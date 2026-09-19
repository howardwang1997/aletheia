"""The kernel-command service artifacts freeze the driver's caller principal.

The driver's RPC clients sign under config.process_principal_id (arl2_runtime
builds each ControllerWorkerRPCClient with it), and the deployments authoring
writes that field as DRIVER_PRINCIPAL. The two kernel-command services used
to freeze WORKER_PRINCIPAL in both their config bodies and their deployment
records, even though every other value in their block came from the driver
side, so the deployed server refused every sign request with "RPC request
differs from its deployment pin". The server closes without a frame, and the
driver only reported "RPC response is not one canonical frame". The
wire-level round-trip test cannot catch this class: it feeds one fixture
value to both sides of the exchange. These tests pin the authored config
header, the deployment record, and the driver's own runtime config to one
principal so the three authoring sites cannot drift apart.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[2]
_DEPLOYMENTS = _REPO / "scripts" / "author-arl2-deployments.py"


def _load_deployments():
    spec = importlib.util.spec_from_file_location(
        "author_arl2_deployments_caller_test", _DEPLOYMENTS
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _authored_driver_principal(deployments) -> str:
    """Resolve the constant the driver config's process_principal_id names."""
    tree = ast.parse(_DEPLOYMENTS.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Name)
            and func.id == "ARL2QuestionCampaignRuntimeConfigV1"
        ):
            for keyword in node.keywords:
                if keyword.arg == "process_principal_id":
                    assert isinstance(keyword.value, ast.Name)
                    return getattr(deployments, keyword.value.id)
    raise AssertionError("ARL2QuestionCampaignRuntimeConfigV1 call not found")


def test_kernel_command_sides_freeze_one_caller_principal() -> None:
    deployments = _load_deployments()
    controller_paths = {"controller_id": "rctl_test", "manifest_sha256": "m" * 64}
    closure = {
        "driver_pins": {
            name: SimpleNamespace(service_id=f"rpcs_{index}", pin_sha256=f"{index:064d}")
            for index, name in enumerate(deployments.COMMAND_SERVICES)
        }
    }

    assert deployments.COMMAND_SERVICES == (
        "action_kernel_command",
        "transition_kernel_command",
    )
    # the driver's clients sign under the runtime config's
    # process_principal_id, which this same script authors as
    # DRIVER_PRINCIPAL; every kernel-command artifact must freeze that
    # same caller or the first real sign is refused at the
    # deployment-pin compare
    assert _authored_driver_principal(deployments) == deployments.DRIVER_PRINCIPAL
    for service in deployments.COMMAND_SERVICES:
        header = deployments._command_service_header(controller_paths, closure, service)
        assert header["worker_process_principal_id"] == deployments.DRIVER_PRINCIPAL
        assert header["controller_id"] == controller_paths["controller_id"]
        assert header["controller_manifest_sha256"] == controller_paths["manifest_sha256"]
        pin = closure["driver_pins"][service]
        assert header["service_id"] == pin.service_id
        assert header["service_pin_sha256"] == pin.pin_sha256
        # the deployment record is the value the live server enforces
        # (rpc_runtime builds the service from it), and the composition
        # factory refuses a config that disagrees with it — a config-only
        # or deployment-only change fails closed at service start
        assert (
            deployments._deployment_worker_principal(service)
            == header["worker_process_principal_id"]
        )


def test_other_rpc_services_keep_the_worker_caller() -> None:
    deployments = _load_deployments()

    # the worker-facing and cuprate services keep WORKER_PRINCIPAL: the
    # worker's clients sign under its own runtime config, which this
    # script authors with WORKER_PRINCIPAL
    others = ("atomic_admission", deployments.CUPRATE_SERVICE)
    for service in others:
        assert deployments._deployment_worker_principal(service) == deployments.WORKER_PRINCIPAL
