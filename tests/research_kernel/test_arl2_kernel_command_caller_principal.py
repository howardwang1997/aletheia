"""The kernel-command service configs freeze the driver's caller principal.

The driver's RPC clients sign under config.process_principal_id (arl2_runtime
builds each ControllerWorkerRPCClient with it), and the deployments authoring
writes that field as DRIVER_PRINCIPAL. The two kernel-command service configs
used to freeze WORKER_PRINCIPAL instead — every other value in their block
already came from the driver side — so the deployed server refused every sign
request with "RPC request differs from its deployment pin"; the server closes
without a frame, and the driver only reported "RPC response is not one
canonical frame". The wire-level round-trip test cannot catch this class: it
feeds one fixture value to both sides of the exchange. This test pins the
authored header against the driver's own principal directly.
"""

from __future__ import annotations

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


def test_kernel_command_header_freezes_the_driver_caller_principal() -> None:
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
    for service in deployments.COMMAND_SERVICES:
        header = deployments._command_service_header(controller_paths, closure, service)
        # the driver's clients sign under the runtime config's
        # process_principal_id, which this same script authors as
        # DRIVER_PRINCIPAL; the service must freeze that same caller or the
        # first real sign is refused at the deployment-pin compare
        assert header["worker_process_principal_id"] == deployments.DRIVER_PRINCIPAL
        assert header["controller_id"] == controller_paths["controller_id"]
        assert header["controller_manifest_sha256"] == controller_paths["manifest_sha256"]
        pin = closure["driver_pins"][service]
        assert header["service_id"] == pin.service_id
        assert header["service_pin_sha256"] == pin.pin_sha256
