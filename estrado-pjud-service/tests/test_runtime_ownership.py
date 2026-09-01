"""Real consumers and flock; only browser/HTTP/DB resources are synthetic."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from playwright.async_api import Error as PlaywrightError

from app.minter import CookieMinter
from app.ojv.session import OjvSession
from worker.__main__ import safe_initialize_pool
from worker.import_jobs import ImportDiscoveryWorker
from worker.maintenance_store import AdmissionClosed
from tests.helpers import legacy_runtime_fence
from tests.test_maintenance_wiring import hold, assert_held, assert_quiescent
from tests.test_minter import _FakeContext, _FakeBrowser
from tests.test_ojv_browser_login import _Page, _Context, _Browser


class Runtime:
    """Driver work is independent of its enter/exit waiter, as in Playwright."""
    def __init__(self, browser, phase, exit_mode):
        self.browser, self.phase, self.exit_mode = browser, phase, exit_mode
        self.chromium = SimpleNamespace(launch=self.launch)
        self.stopped, self.exiting = asyncio.Event(), asyncio.Event()
        self.phase_started = asyncio.Event()
        self.driver = None

    async def __aenter__(self):
        self.driver = asyncio.create_task(self.stopped.wait())
        if self.phase == "enter_wait":
            self.phase_started.set()
            await asyncio.Event().wait()
        if self.phase == "enter":
            raise PlaywrightError("synthetic enter failure")
        return self

    async def launch(self, **kwargs):
        if self.phase == "launch_wait":
            self.phase_started.set()
            await asyncio.Event().wait()
        if self.phase == "launch":
            raise PlaywrightError("synthetic launch failure")
        return self.browser

    async def __aexit__(self, *args):
        self.exiting.set()
        if self.exit_mode == "error":
            raise RuntimeError("synthetic driver exit failure")
        if self.exit_mode == "ok":
            self.stopped.set()
        await asyncio.shield(self.driver)

    async def finish(self):
        self.stopped.set()
        if self.driver:
            await self.driver


def consumer(monkeypatch, kind, phase, exit_mode):
    if kind == "minter":
        browser = _FakeBrowser(_FakeContext())
        runtime = Runtime(browser, phase, exit_mode)
        monkeypatch.setattr("app.minter.async_playwright", lambda: runtime)
        pool = SimpleNamespace(initialize=CookieMinter("https://example.invalid").mint)
        return runtime, lambda: safe_initialize_pool(
            pool, max_retries=1, runtime_fence=legacy_runtime_fence(),
        )

    page = _Page()
    # A business result absorbed by the actual import consumer, after cleanup.
    async def rejected(*args, **kwargs):
        return SimpleNamespace(status=500)
    page.goto = rejected
    browser = _Browser(_Context(page))
    runtime = Runtime(browser, phase, exit_mode)
    monkeypatch.setattr("app.ojv.browser_login.async_playwright", lambda: runtime)
    def session_factory(proxy_url, cookies, user_agent, **kwargs):
        def forbidden(request):
            pytest.fail("unexpected HTTP")
        return OjvSession(proxy_url=proxy_url, cookies=cookies, user_agent=user_agent,
                          rate_limit_s=0, transport=httpx.MockTransport(forbidden))
    @asynccontextmanager
    async def track(**kwargs):
        yield
    bundle = SimpleNamespace(proxy_url=None, cookies={}, user_agent="official-test-agent")
    pool = SimpleNamespace(acquire_familia_bundle=AsyncMock(return_value=(bundle, 0)),
                           release_familia_bundle=AsyncMock())
    worker = ImportDiscoveryWorker(supabase=None, pool=pool, worker_id="test",
        fetch_credential=AsyncMock(), session_factory=session_factory,
        proxy_usage=SimpleNamespace(enabled=True, track=track))
    worker._validate_credential_revision = AsyncMock()
    job = SimpleNamespace(job_id=uuid4(), law_firm_id=uuid4(), credential_id=uuid4(),
                          claim_token=uuid4(), matters=(), include_closed=False)
    return runtime, lambda: worker._discover_once(job, {
        "binding_version": "test", "rut": "11111111-1", "password": "synthetic"}, 1)


@pytest.mark.parametrize("kind", ["official", "minter"])
@pytest.mark.parametrize("phase", ["enter", "launch", "body"])
async def test_absorbed_driver_exit_failure_retains_real_flock(worker_maintenance, monkeypatch, kind, phase):
    runtime, run = consumer(monkeypatch, kind, phase, "error")
    if kind == "official" and phase == "body":
        for resource in (runtime.browser, runtime.browser.context, runtime.browser.context.page):
            resource.close = AsyncMock(side_effect=PlaywrightError("synthetic close failure"))
    try:
        result = await worker_maintenance.run(run)
        assert result is False if kind == "minter" else result.status in {"timeout", "upstream_changed"}
        assert not runtime.driver.done()
        hold(worker_maintenance)
        assert worker_maintenance.uncertain
        assert worker_maintenance.publish_ack().state == "draining"
        with pytest.raises(AdmissionClosed), worker_maintenance.store.exclusive_lease():
            pass
    finally:
        await runtime.finish()


@pytest.mark.parametrize("kind", ["official", "minter"])
@pytest.mark.parametrize("phase", ["enter", "launch", "body"])
async def test_confirmed_runtime_exit_preserves_business_outcome_and_quiescence(worker_maintenance, monkeypatch, kind, phase):
    runtime, run = consumer(monkeypatch, kind, phase, "ok")
    try:
        result = await worker_maintenance.run(run)
        assert result is (phase == "body") if kind == "minter" else result.status in {"timeout", "upstream_changed"}
        assert runtime.driver.done()
        hold(worker_maintenance)
        assert not worker_maintenance.uncertain
        assert_quiescent(worker_maintenance)
    finally:
        await runtime.finish()


@pytest.mark.parametrize("kind", ["official", "minter"])
@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_driver_timeout_or_repeat_cancel_never_abandons_exit(worker_maintenance, monkeypatch, kind, repeat_cancel):
    runtime, run = consumer(monkeypatch, kind, "body", "wait")
    # Lower only the real cleanup deadline; no browser or slow external effect.
    monkeypatch.setattr("app.ojv.browser_login._CLEANUP_TIMEOUT_S", 0.03)
    monkeypatch.setattr("app.minter._CLEANUP_TIMEOUT_S", 0.03)
    running = asyncio.create_task(worker_maintenance.run(run))
    try:
        await asyncio.wait_for(runtime.exiting.wait(), 1)
        hold(worker_maintenance)
        if repeat_cancel:
            for _ in range(3):
                running.cancel()
                await asyncio.sleep(0)
        await asyncio.sleep(0.08)
        assert not runtime.driver.done()
        assert not running.done()
        assert_held(worker_maintenance)
        assert worker_maintenance.uncertain
    finally:
        await runtime.finish()
        await asyncio.gather(running, return_exceptions=True)
    assert worker_maintenance.publish_ack().state == "draining"
    with pytest.raises(AdmissionClosed), worker_maintenance.store.exclusive_lease():
        pass


@pytest.mark.parametrize("kind", ["official", "minter"])
@pytest.mark.parametrize("phase", ["enter_wait", "launch_wait"])
async def test_partial_runtime_cancellation_joins_real_exit(worker_maintenance, monkeypatch, kind, phase):
    runtime, run = consumer(monkeypatch, kind, phase, "wait")
    running = asyncio.create_task(worker_maintenance.run(run))
    try:
        await asyncio.wait_for(runtime.phase_started.wait(), 1)
        hold(worker_maintenance)
        running.cancel()
        await asyncio.wait_for(runtime.exiting.wait(), 1)
        for _ in range(3):
            running.cancel()
            await asyncio.sleep(0)
        assert not runtime.driver.done()
        assert not running.done()
        assert_held(worker_maintenance)
    finally:
        await runtime.finish()
        await asyncio.gather(running, return_exceptions=True)
    assert worker_maintenance.uncertain
    assert worker_maintenance.publish_ack().state == "draining"


async def test_official_resource_close_failure_resolved_by_confirmed_driver_exit(worker_maintenance, monkeypatch):
    runtime, run = consumer(monkeypatch, "official", "body", "ok")
    runtime.browser.close = AsyncMock(side_effect=PlaywrightError("synthetic close failure"))
    try:
        assert (await worker_maintenance.run(run)).status == "timeout"
        assert runtime.driver.done()
        hold(worker_maintenance)
        assert not worker_maintenance.uncertain
        assert_quiescent(worker_maintenance)
    finally:
        await runtime.finish()


@pytest.mark.parametrize("kind", ["official", "minter"])
async def test_inherited_closed_context_rejected_before_driver_creation(worker_maintenance, monkeypatch, kind):
    runtime, run = consumer(monkeypatch, kind, "body", "ok")
    begin = asyncio.Event()
    async def late():
        await begin.wait()
        return await run()
    async def parent():
        return asyncio.create_task(late())
    child = await worker_maintenance.run(parent)
    begin.set()
    await asyncio.gather(child, return_exceptions=True)
    assert runtime.driver is None
    assert worker_maintenance.uncertain


def test_outside_admission_returns_original_manager_unchanged():
    from app.playwright_runtime import owned_playwright
    manager = object()
    assert owned_playwright(lambda: manager, cleanup_timeout=0.01) is manager


async def test_configuration_failure_before_enter_has_no_runtime_uncertainty(worker_maintenance, monkeypatch):
    from pydantic import SecretStr
    from app.ojv.browser_login import login_official_ojv
    from app.ojv.errors import OjvUpstreamChangedError
    runtime = Runtime(None, "body", "error")
    monkeypatch.setattr("app.ojv.browser_login.async_playwright", lambda: runtime)
    async def operation():
        with pytest.raises(OjvUpstreamChangedError):
            await login_official_ojv(SecretStr("11111111-1"), SecretStr("synthetic"),
                proxy_url="http://proxy.invalid:invalid-port", user_agent="test")
    await worker_maintenance.run(operation)
    assert runtime.driver is None
    hold(worker_maintenance)
    assert not worker_maintenance.uncertain
    assert_quiescent(worker_maintenance)
