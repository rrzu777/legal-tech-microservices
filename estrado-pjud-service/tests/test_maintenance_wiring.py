"""Actual admission locks around worker side effects; all endpoints are local fakes."""
import asyncio
from dataclasses import replace
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from worker.config import run_query
from worker.maintenance_store import AdmissionClosed, MaintenanceError


def hold(worker):
    control = worker.store.read_control()
    worker.store.transition(control.operation_id, "open",
                            replace(control, state="hold", operation_id=str(uuid4())))
    return worker.publish_ack()


def assert_held(worker):
    assert worker.inflight == 1
    assert worker.publish_ack().state == "draining"
    with pytest.raises(AdmissionClosed):
        with worker.store.exclusive_lease():
            pytest.fail("exclusive acquired before complete operation")


def assert_quiescent(worker):
    assert worker.inflight == 0
    assert worker.publish_ack().state == "quiescent"
    with worker.store.exclusive_lease():
        pass


def thread_operation(kind, execute):
    if kind == "query":
        return lambda: run_query(SimpleNamespace(execute=execute))
    from app.r2 import R2Client
    client = object.__new__(R2Client)
    client._bucket = "fake-bucket"
    client._s3 = SimpleNamespace(put_object=execute, head_object=execute,
                                exceptions=SimpleNamespace(ClientError=ValueError))
    if kind == "upload":
        return lambda: client.upload("fake.pdf", b"pdf", "application/pdf")
    return lambda: client.exists("fake.pdf")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["query", "upload", "exists"])
async def test_cancelled_wrapper_keeps_real_thread_lease(worker_maintenance, kind):
    worker = worker_maintenance
    started, finish = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    def execute(**_kwargs):
        loop.call_soon_threadsafe(started.set)
        assert finish.wait(3), "test did not release its local thread"
        return 7
    running = asyncio.create_task(worker.run(thread_operation(kind, execute)))
    try:
        await asyncio.wait_for(started.wait(), 1)
        hold(worker)
        running.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert_held(worker)
        assert not running.done()
    finally:
        finish.set()
        await asyncio.gather(running, return_exceptions=True)
    assert worker.uncertain
    assert worker.publish_ack().state == "draining"


@pytest.mark.asyncio
async def test_minter_cleanup_survives_parent_cancellation(worker_maintenance, monkeypatch):
    from tests.test_minter import _playwright_factory
    from app.minter import CookieMinter
    context, browser, factory = _playwright_factory()
    started, finish, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def close():
        started.set()
        await finish.wait()
        closed.set()
    browser.close = close
    monkeypatch.setattr("app.minter.async_playwright", factory)
    worker = worker_maintenance
    running = asyncio.create_task(worker.run(CookieMinter("https://example.invalid").mint))
    try:
        await asyncio.wait_for(started.wait(), 1)
        hold(worker)
        running.cancel()
        await asyncio.sleep(0)
        finish.set()
        await asyncio.gather(running, return_exceptions=True)
        assert closed.is_set()
        assert worker.uncertain
    finally:
        finish.set()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_minter_one_cleanup_error_does_not_abandon_other_cleanup(worker_maintenance, monkeypatch):
    from tests.test_minter import _playwright_factory
    from app.minter import CookieMinter
    context, browser, factory = _playwright_factory()
    started, finish, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def detach():
        started.set()
        await finish.wait()
        closed.set()
    context.cdp.detach = detach
    browser.close = AsyncMock(side_effect=RuntimeError("fake unexpected browser close failure"))
    monkeypatch.setattr("app.minter.async_playwright", factory)
    worker = worker_maintenance
    async def operation():
        try:
            await CookieMinter("https://example.invalid").mint()
        except RuntimeError:
            # Pool initialization catches mint failures; cleanup uncertainty
            # must not depend on an exception escaping that orchestration layer.
            pass
    running = asyncio.create_task(worker.run(operation))
    try:
        await asyncio.wait_for(started.wait(), 1)
        hold(worker)
        for _ in range(10):
            await asyncio.sleep(0)
        assert_held(worker)
        assert not running.done()
    finally:
        finish.set()
        await asyncio.gather(running, return_exceptions=True)
    assert closed.is_set()
    assert worker.uncertain


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["candidate", "retired", "browser", "cdp", "mint"])
async def test_late_cleanup_or_mint_context_rejects_before_side_effect(worker_maintenance, monkeypatch, kind):
    from app.minter import CookieMinter
    from worker.session_pool import SessionPool
    worker = worker_maintenance
    release = asyncio.Event()
    calls = []
    async def effect():
        calls.append(kind)
    resource = SimpleNamespace(close=effect, detach=effect)
    minter = CookieMinter("https://example.invalid")
    pool = object.__new__(SessionPool)
    pool._retired_cleanup_tasks = set()
    def launch_factory():
        calls.append("launch")
        raise RuntimeError("fake browser must not launch")
    monkeypatch.setattr("app.minter.async_playwright", launch_factory)
    async def late():
        await release.wait()
        if kind == "candidate":
            await pool._close_candidate(resource)
        elif kind == "retired":
            pool._retire_session(resource, 0)
        elif kind == "browser":
            await minter._close_playwright_resource(resource, "browser")
        elif kind == "cdp":
            await minter._detach_cdp_session(resource)
        else:
            await minter.mint()
    async def body():
        return asyncio.create_task(late())
    child = await worker.run(body)
    hold(worker)
    with worker.store.exclusive_lease():
        release.set()
        result = await asyncio.gather(child, return_exceptions=True)
        await asyncio.gather(*pool._retired_cleanup_tasks)
        assert isinstance(result[0], MaintenanceError)
        assert calls == []


@pytest.mark.asyncio
async def test_watcher_failure_disables_future_admission(worker_maintenance, monkeypatch):
    from worker.__main__ import watch_maintenance
    worker = worker_maintenance
    hold(worker)
    shutdown = asyncio.Event()
    original = worker.publish_ack
    def failed_write():
        shutdown.set()
        raise RuntimeError("fake writer failure")
    monkeypatch.setattr(worker, "publish_ack", failed_write)
    await watch_maintenance(worker, shutdown, poll_interval=0.001)
    monkeypatch.setattr(worker, "publish_ack", original)
    assert worker.uncertain
    assert worker.publish_ack().state == "draining"
    with pytest.raises(AdmissionClosed):
        await worker.run(lambda: pytest.fail("work admitted after watcher failure"))


@pytest.mark.asyncio
async def test_case_error_swallowed_by_batch_marks_unsafe(worker_maintenance):
    from worker.__main__ import process_batch
    engine = SimpleNamespace(sync_case=AsyncMock(side_effect=RuntimeError("unknown case outcome")))
    await worker_maintenance.run(lambda: process_batch(
        [{"id": "fake"}], engine, 1, asyncio.Event(), MagicMock(is_open=False),
        processing_window=lambda: True,
    ))
    hold(worker_maintenance)
    assert worker_maintenance.uncertain


@pytest.mark.asyncio
async def test_r2_known_missing_object_does_not_poison_admission(worker_maintenance):
    from app.r2 import R2Client
    from botocore.exceptions import ClientError
    def head(**kwargs):
        raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
    client = object.__new__(R2Client)
    client._bucket = "fake-bucket"
    client._s3 = SimpleNamespace(head_object=head, exceptions=SimpleNamespace(ClientError=ClientError))
    assert await worker_maintenance.run(lambda: client.exists("absent.pdf")) is False
    hold(worker_maintenance)
    assert_quiescent(worker_maintenance)


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", [("AccessDenied", 403), ("InternalError", 500)])
async def test_r2_unknown_head_outcome_returns_false_but_keeps_ex_blocked(worker_maintenance, code, status):
    from app.r2 import R2Client
    from botocore.exceptions import ClientError
    def head(**kwargs):
        assert kwargs == {"Bucket": "fake-bucket", "Key": "unknown.pdf"}
        raise ClientError({"Error": {"Code": code},
                           "ResponseMetadata": {"HTTPStatusCode": status}}, "HeadObject")
    client = object.__new__(R2Client)
    client._bucket = "fake-bucket"
    client._s3 = SimpleNamespace(head_object=head, exceptions=SimpleNamespace(ClientError=ClientError))
    assert await worker_maintenance.run(lambda: client.exists("unknown.pdf")) is False
    hold(worker_maintenance)
    assert worker_maintenance.uncertain
    assert worker_maintenance.inflight == 0
    assert worker_maintenance.publish_ack().state == "draining"
    with pytest.raises(AdmissionClosed):
        with worker_maintenance.store.exclusive_lease():
            pytest.fail("unknown R2 outcome released its safety lease")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["query", "upload", "exists"])
async def test_late_inherited_thread_is_rejected_before_submission(worker_maintenance, kind):
    worker = worker_maintenance
    release = asyncio.Event()
    calls = []
    async def late():
        await release.wait()
        return await thread_operation(kind, lambda **kw: calls.append(kw))()
    async def body():
        return asyncio.create_task(late())
    child = await worker.run(body)
    hold(worker)
    with worker.store.exclusive_lease():
        release.set()
        outcome = await asyncio.gather(child, return_exceptions=True)
        assert isinstance(outcome[0], MaintenanceError)
        assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["candidate", "retired", "browser", "cdp"])
async def test_swallowed_cleanup_error_marks_admission_uncertain(worker_maintenance, kind):
    from worker.session_pool import SessionPool
    from app.minter import CookieMinter, PlaywrightError
    worker = worker_maintenance
    resource = SimpleNamespace(close=AsyncMock(side_effect=PlaywrightError("fake close")),
                               detach=AsyncMock(side_effect=RuntimeError("fake detach")))
    pool = object.__new__(SessionPool)
    if kind == "candidate":
        operation = lambda: pool._close_candidate(resource)
    elif kind == "retired":
        operation = lambda: pool._close_retired_session(resource, 0)
    elif kind == "browser":
        operation = lambda: CookieMinter("https://example.invalid")._close_playwright_resource(resource, "browser")
    else:
        operation = lambda: CookieMinter("https://example.invalid")._detach_cdp_session(resource)
    await worker.run(operation)
    hold(worker)
    assert worker.uncertain
    assert worker.publish_ack().state == "draining"


@pytest.mark.asyncio
async def test_retired_session_remains_owned_until_close_finishes(worker_maintenance):
    from worker.session_pool import SessionPool
    worker = worker_maintenance
    started, finish = asyncio.Event(), asyncio.Event()
    async def close():
        started.set()
        await finish.wait()
    pool = object.__new__(SessionPool)
    pool._retired_cleanup_tasks = set()
    async def operation():
        pool._retire_session(SimpleNamespace(close=close), 0)
    running = asyncio.create_task(worker.run(operation))
    try:
        await asyncio.wait_for(started.wait(), 1)
        hold(worker)
        assert_held(worker)
    finally:
        finish.set()
        await running
        await asyncio.gather(*pool._retired_cleanup_tasks)
    assert_quiescent(worker)


@pytest.mark.asyncio
async def test_import_hold_during_claim_owns_finalize_and_capacity_release(worker_maintenance):
    from worker.__main__ import run_import_discovery_loop
    worker = worker_maintenance
    started, finish_claim, finalizing, finish_finalize, shutdown = [asyncio.Event() for _ in range(5)]
    phases = []
    async def process_import_job():
        phases.append("claim")
        started.set()
        await finish_claim.wait()
        phases.append("finalize")
        finalizing.set()
        await finish_finalize.wait()
        phases.append("capacity_released")
        return True
    running = asyncio.create_task(run_import_discovery_loop(
        SimpleNamespace(process_import_job=process_import_job), MagicMock(), shutdown,
        poll_interval=0.001, maintenance=worker,
    ))
    try:
        await asyncio.wait_for(started.wait(), 1)
        hold(worker)
        assert_held(worker)
        finish_claim.set()
        await asyncio.wait_for(finalizing.wait(), 1)
        assert_held(worker)
        finish_finalize.set()
        # Wait for the complete outer admission to finish, not merely the body.
        while worker.inflight:
            await asyncio.sleep(0)
        assert_quiescent(worker)
        assert phases == ["claim", "finalize", "capacity_released"]
    finally:
        shutdown.set()
        finish_claim.set()
        finish_finalize.set()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_process_batch_parent_cancellation_joins_case_cleanup(worker_maintenance):
    from worker.__main__ import process_batch
    worker = worker_maintenance
    started, finish, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def sync_case(case):
        try:
            started.set()
            await finish.wait()
        finally:
            cleaned.set()
    async def operation():
        await process_batch([{"id": "one"}], SimpleNamespace(sync_case=sync_case), 1,
                            asyncio.Event(), MagicMock(is_open=False), processing_window=lambda: True)
    running = asyncio.create_task(worker.run(operation))
    try:
        await started.wait()
        hold(worker)
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        assert cleaned.is_set()
        assert worker.uncertain
    finally:
        finish.set()
