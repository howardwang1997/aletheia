from __future__ import annotations

from aletheia.execution.ports import ArtifactStorePort, ExecutionReceiptPort


class _ArtifactPortShape:
    def verify_manifest(self, *, intent, manifest):  # type: ignore[no-untyped-def]
        return ()

    def load_verified_receipt(self, *, verified_receipt_sha256):  # type: ignore[no-untyped-def]
        return None


class _ReceiptPortShape:
    def append_receipt(self, *, receipt, expected_previous_receipt_sha256):  # type: ignore[no-untyped-def]
        return receipt

    def load_latest_receipt(self, *, execution_id, infrastructure_attempt_id):  # type: ignore[no-untyped-def]
        return None

    def list_attempt_receipts(self, *, execution_id, infrastructure_attempt_id):  # type: ignore[no-untyped-def]
        return ()


def test_ports_are_structural_and_have_no_implementation_dependency() -> None:
    assert isinstance(_ArtifactPortShape(), ArtifactStorePort)
    assert isinstance(_ReceiptPortShape(), ExecutionReceiptPort)
