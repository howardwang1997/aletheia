from __future__ import annotations

import hashlib

import pytest

import aletheia.knowledge as k
from .f8s2_fixtures import (
    StepClock,
    SyntheticSearchAdapter,
    build_adapters,
    build_search_plan,
    sha,
)
from .test_schema_spike import _time


@pytest.mark.asyncio
async def test_all_sources_execute_commit_load_and_replay_exactly(tmp_path) -> None:
    plan = build_search_plan()
    adapters = build_adapters(plan)
    archive = k.ContentAddressedResponseArchive(tmp_path / "responses")
    executor = k.SearchExecutor(
        archive=archive, adapters=adapters, clock=StepClock()
    )

    committed = await executor.execute_and_commit(
        plan=plan, execution_id="f8s2-successful-execution"
    )
    execution = committed.execution
    assert execution.coverage_disposition == "eligible"
    assert not execution.failures
    assert len(execution.page_receipts) == len(plan.queries)
    assert sum(len(adapter.fetch_calls) for adapter in adapters.values()) == len(plan.queries)
    assert k.load_search_execution(archive=archive, ledger=committed.ledger) == execution

    audit = k.replay_search_execution(
        execution=execution,
        archive=archive,
        adapters=adapters,
        audited_at=_time("2024-12-31T00:00:00Z"),
    )
    assert audit.status is k.ReplayAuditStatus.COMPLETE
    assert {item.status for item in audit.receipts} == {k.ReplayItemStatus.VERIFIED}


@pytest.mark.asyncio
async def test_circuit_open_is_recorded_and_does_not_skip_later_queries(tmp_path) -> None:
    plan = build_search_plan()
    adapters = build_adapters(plan)
    target = plan.queries[0]
    adapters[target.source_id].fetch_errors[target.logical_query_id] = k.CircuitOpenError()
    archive = k.ContentAddressedResponseArchive(tmp_path / "responses")
    execution = await k.SearchExecutor(
        archive=archive, adapters=adapters, clock=StepClock()
    ).execute(plan=plan, execution_id="f8s2-circuit-open")

    assert execution.coverage_disposition == "blocked"
    assert execution.session.stopping_reason is k.SearchStoppingReason.HARD_FAILURE
    assert [failure.kind for failure in execution.failures] == [
        k.SearchFailureKind.CIRCUIT_OPEN
    ]
    assert len({receipt.logical_query_sha256 for receipt in execution.page_receipts}) == len(
        plan.queries
    )
    assert sum(len(adapter.fetch_calls) for adapter in adapters.values()) == len(plan.queries)

    audit = k.replay_search_execution(
        execution=execution,
        archive=archive,
        adapters=adapters,
        audited_at=_time("2024-12-31T00:00:00Z"),
    )
    assert audit.status is k.ReplayAuditStatus.INCOMPLETE
    assert audit.receipts[0].status is k.ReplayItemStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_parse_failure_keeps_response_and_replays_the_same_failure(tmp_path) -> None:
    plan = build_search_plan()
    adapters = build_adapters(plan)
    target = plan.queries[0]
    adapters[target.source_id].parse_errors[target.logical_query_id] = ValueError(
        "synthetic parser rejection"
    )
    archive = k.ContentAddressedResponseArchive(tmp_path / "responses")
    execution = await k.SearchExecutor(
        archive=archive, adapters=adapters, clock=StepClock()
    ).execute(plan=plan, execution_id="f8s2-parse-failure")

    first = execution.page_receipts[0]
    assert first.response is not None
    assert execution.failures[0].kind is k.SearchFailureKind.PARSE_ERROR
    assert execution.failures[0].response_sha256 == first.response.response_sha256
    audit = k.replay_search_execution(
        execution=execution,
        archive=archive,
        adapters=adapters,
        audited_at=_time("2024-12-31T00:00:00Z"),
    )
    assert audit.status is k.ReplayAuditStatus.INCOMPLETE
    assert audit.receipts[0].status is k.ReplayItemStatus.VERIFIED


@pytest.mark.asyncio
async def test_text_policy_violation_and_http_429_fail_closed(tmp_path) -> None:
    plan = build_search_plan()
    adapters = build_adapters(plan)
    first, second = plan.queries[:2]
    adapters[first.source_id].forbidden_text_queries.add(first.logical_query_id)
    adapters[second.source_id].status_codes[second.logical_query_id] = 429
    archive = k.ContentAddressedResponseArchive(tmp_path / "responses")
    execution = await k.SearchExecutor(
        archive=archive, adapters=adapters, clock=StepClock()
    ).execute(plan=plan, execution_id="f8s2-policy-and-rate-limit")

    assert execution.coverage_disposition == "blocked"
    assert {failure.kind for failure in execution.failures} == {
        k.SearchFailureKind.POLICY_VIOLATION,
        k.SearchFailureKind.RATE_LIMITED,
    }
    failed_receipts = {
        receipt.request_id: receipt
        for receipt in execution.page_receipts
        if receipt.outcome is k.QueryOutcome.ERROR
    }
    assert all(receipt.response is None for receipt in failed_receipts.values())


@pytest.mark.asyncio
async def test_cursor_pages_are_ordered_bounded_and_replayable(tmp_path) -> None:
    plan = build_search_plan(max_pages=2)
    adapters = build_adapters(plan)
    target = plan.queries[0]
    adapters[target.source_id].page_counts[target.logical_query_id] = 2
    archive = k.ContentAddressedResponseArchive(tmp_path / "responses")
    execution = await k.SearchExecutor(
        archive=archive, adapters=adapters, clock=StepClock()
    ).execute(plan=plan, execution_id="f8s2-two-page-success")

    target_receipts = [
        receipt
        for receipt in execution.page_receipts
        if receipt.logical_query_sha256 == target.logical_query_sha256
    ]
    assert [receipt.page_index for receipt in target_receipts] == [0, 1]
    assert target_receipts[0].terminal is False
    assert target_receipts[0].output_page_token_sha256 is not None
    assert target_receipts[1].terminal is True
    assert execution.coverage_disposition == "eligible"
    assert k.replay_search_execution(
        execution=execution,
        archive=archive,
        adapters=adapters,
        audited_at=_time("2024-12-31T00:00:00Z"),
    ).status is k.ReplayAuditStatus.COMPLETE


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["duplicate", "never_terminal"])
async def test_adversarial_pagination_blocks_coverage_but_finishes_other_sources(
    tmp_path, failure_mode: str
) -> None:
    plan = build_search_plan(max_pages=2)
    adapters = build_adapters(plan)
    target = plan.queries[0]
    adapter = adapters[target.source_id]
    if failure_mode == "duplicate":
        adapter.page_counts[target.logical_query_id] = 2
        adapter.repeat_across_pages.add(target.logical_query_id)
        expected = k.SearchFailureKind.DUPLICATE_PAGE_HIT
    else:
        adapter.never_terminal.add(target.logical_query_id)
        expected = k.SearchFailureKind.PAGINATION_INCOMPLETE
    archive = k.ContentAddressedResponseArchive(tmp_path / failure_mode)
    execution = await k.SearchExecutor(
        archive=archive, adapters=adapters, clock=StepClock()
    ).execute(plan=plan, execution_id=f"f8s2-{failure_mode}")

    assert expected in {failure.kind for failure in execution.failures}
    assert execution.coverage_disposition == "blocked"
    assert len({receipt.logical_query_sha256 for receipt in execution.page_receipts}) == len(
        plan.queries
    )
    audit = k.replay_search_execution(
        execution=execution,
        archive=archive,
        adapters=adapters,
        audited_at=_time("2024-12-31T00:00:00Z"),
    )
    assert audit.status is k.ReplayAuditStatus.INCOMPLETE
    failed_index = next(
        index
        for index, receipt in enumerate(execution.page_receipts)
        if receipt.outcome is k.QueryOutcome.ERROR
    )
    assert audit.receipts[failed_index].status is k.ReplayItemStatus.VERIFIED


@pytest.mark.asyncio
async def test_runtime_adapter_drift_is_rejected_before_request(tmp_path) -> None:
    plan = build_search_plan()
    adapters = build_adapters(plan)
    manifest = plan.adapters[0]
    payload = manifest.model_dump(mode="python")
    payload["parser_sha256"] = sha("drifted-parser")
    adapters[manifest.source_id] = SyntheticSearchAdapter(
        k.ProviderAdapterManifest.model_validate(payload)
    )
    executor = k.SearchExecutor(
        archive=k.ContentAddressedResponseArchive(tmp_path / "responses"),
        adapters=adapters,
        clock=StepClock(),
    )
    with pytest.raises(ValueError, match="runtime adapter manifest differs"):
        await executor.execute(plan=plan, execution_id="f8s2-adapter-drift")
    assert not any(adapter.fetch_calls for adapter in adapters.values())


@pytest.mark.asyncio
async def test_archive_tampering_turns_replay_into_mismatch(tmp_path) -> None:
    plan = build_search_plan()
    adapters = build_adapters(plan)
    archive = k.ContentAddressedResponseArchive(tmp_path / "responses")
    execution = await k.SearchExecutor(
        archive=archive, adapters=adapters, clock=StepClock()
    ).execute(plan=plan, execution_id="f8s2-tamper-test")
    response = execution.page_receipts[0].response
    assert response is not None
    target = archive.root / response.relative_path
    target.chmod(0o600)
    target.write_bytes(b"tampered metadata")

    audit = k.replay_search_execution(
        execution=execution,
        archive=archive,
        adapters=adapters,
        audited_at=_time("2024-12-31T00:00:00Z"),
    )
    assert audit.status is k.ReplayAuditStatus.MISMATCH
    assert audit.receipts[0].status is k.ReplayItemStatus.MISMATCH


@pytest.mark.asyncio
async def test_executor_enforces_manifest_request_interval(tmp_path) -> None:
    plan = build_search_plan(minimum_request_interval_seconds=1.0)
    adapters = build_adapters(plan)
    delays: list[float] = []

    async def capture_delay(value: float) -> None:
        delays.append(value)

    execution = await k.SearchExecutor(
        archive=k.ContentAddressedResponseArchive(tmp_path / "responses"),
        adapters=adapters,
        clock=StepClock(),
        sleeper=capture_delay,
    ).execute(plan=plan, execution_id="f8s2-paced-execution")
    assert execution.coverage_disposition == "eligible"
    assert delays
    assert all(delay > 0 for delay in delays)


@pytest.mark.asyncio
async def test_failure_details_are_hash_only_not_raw_messages(tmp_path) -> None:
    plan = build_search_plan()
    adapters = build_adapters(plan)
    target = plan.queries[0]
    secret_like_message = "provider failure containing sensitive opaque value"
    adapters[target.source_id].fetch_errors[target.logical_query_id] = RuntimeError(
        secret_like_message
    )
    execution = await k.SearchExecutor(
        archive=k.ContentAddressedResponseArchive(tmp_path / "responses"),
        adapters=adapters,
        clock=StepClock(),
    ).execute(plan=plan, execution_id="f8s2-hashed-error-detail")
    serialized = execution.model_dump_json()
    assert secret_like_message not in serialized
    assert hashlib.sha256(secret_like_message.encode()).hexdigest() not in serialized
