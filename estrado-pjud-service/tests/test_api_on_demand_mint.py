import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import Settings
from app.cookie_store import CookieBundle
from app.minter import MintResult
from app.failure_kind import BlockedPageError
from app.metrics import api_metrics
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError
from tests.helpers import FakeOJVSession
from worker.proxy_control import ProxyControlSnapshot


class MemoryCookieStore:
    def __init__(self):
        self.slots: dict[str, CookieBundle] = {}

    def load_all(self):
        return dict(self.slots)

    def save_slot(self, slot_id, cookies, user_agent, proxy_token):
        self.slots[str(slot_id)] = CookieBundle(
            cookies=cookies,
            user_agent=user_agent,
            saved_at=time.time(),
            proxy_token=proxy_token,
        )


class RecordingUsageTracker:
    def __init__(self):
        self.operations: list[str] = []

    @asynccontextmanager
    async def track(self, **kwargs):
        self.operations.append(kwargs["operation"])
        yield SimpleNamespace(retry_count=0)


@pytest.mark.asyncio
async def test_familia_stale_bundle_mints_and_returns_fresh_bundle_on_demand(
    monkeypatch, tmp_path,
):
    """Quitar la recuperación Familia stale→mint→bundle debe romper este test."""
    from app import session_pool as pool_module
    from app.pool_guard import familia_bundle_or_alert
    from app.session_pool import APISessionPool

    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        OJV_PROXY_STICKY_LIFETIME="1h",
        _env_file=None,
    )
    control = AsyncMock()
    control.refresh.return_value = ProxyControlSnapshot(
        allowed=True,
        status="enabled",
        reason_code="interactive",
        revision=1,
        source="database",
    )
    usage = RecordingUsageTracker()
    store = MemoryCookieStore()
    store.slots["0"] = CookieBundle(
        cookies={"TSPD_101": "stale"},
        user_agent="stale-UA",
        saved_at=time.time() - 7201,
        proxy_token="stale-token",
    )
    minted_proxies: list[str | None] = []

    class FreshMinter:
        def __init__(self, _base_url, proxy=None):
            minted_proxies.append(proxy)

        async def mint(self):
            return MintResult(cookies={"TSPD_101": "fresh"}, user_agent="fresh-UA")

    monkeypatch.setattr(pool_module, "CookieMinter", FreshMinter)
    monkeypatch.setattr(pool_module, "OJVHttpAdapter", lambda *_args, **_kwargs: MagicMock())
    monkeypatch.setattr(pool_module, "OJVSession", FakeOJVSession)

    pool = APISessionPool(settings, proxy_control=control, proxy_usage=usage)
    pool._store = store
    request = MagicMock()
    request.app.state.alerter = None

    bundle = await familia_bundle_or_alert(pool, request)

    assert bundle.cookies == {"TSPD_101": "fresh"}
    assert bundle.proxy_url == minted_proxies[0]
    assert len(minted_proxies) == 1
    assert minted_proxies[0] is not None
    assert usage.operations == ["mint"]
    control.refresh.assert_awaited()


@pytest.mark.asyncio
async def test_empty_proxy_pool_mints_and_persists_one_bundle_on_demand(monkeypatch, tmp_path):
    """Removing interactive lazy mint must make this test fail.

    A user action is not scheduled work: with paid traffic enabled and no
    worker bundle yet, the API must mint through the residential proxy instead
    of returning NoUsableBundleError or falling back to the datacenter IP.
    """
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool

    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    control = AsyncMock()
    control.refresh.return_value = ProxyControlSnapshot(
        allowed=True,
        status="enabled",
        reason_code="manual_canary",
        revision=1,
        source="database",
    )
    usage = RecordingUsageTracker()
    store = MemoryCookieStore()
    minted_proxies: list[str | None] = []
    adapter_calls: list[dict] = []

    class FakeMinter:
        def __init__(self, _base_url, proxy=None):
            minted_proxies.append(proxy)

        async def mint(self):
            return MintResult(cookies={"TSPD_101": "fresh"}, user_agent="fresh-UA")

    def fake_adapter(_settings, **kwargs):
        adapter_calls.append(kwargs)
        return MagicMock()

    monkeypatch.setattr(pool_module, "CookieMinter", FakeMinter, raising=False)
    monkeypatch.setattr(pool_module, "OJVHttpAdapter", fake_adapter)
    monkeypatch.setattr(pool_module, "OJVSession", FakeOJVSession)

    pool = APISessionPool(settings, proxy_control=control, proxy_usage=usage)
    pool._store = store

    session = await pool.acquire()

    assert isinstance(session, FakeOJVSession)
    assert len(minted_proxies) == 1
    assert minted_proxies[0] is not None
    assert "geo.iproyal.com:12321" in minted_proxies[0]
    assert len(store.slots) == 1
    assert usage.operations == ["mint"]
    assert adapter_calls == [{
        "proxy": minted_proxies[0],
        "user_agent": "fresh-UA",
        "cookies": {"TSPD_101": "fresh"},
    }]


@pytest.mark.asyncio
async def test_on_demand_mint_rotates_ip_after_f5_challenge(monkeypatch, tmp_path):
    """One challenged residential IP must not fail the user's whole action."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool

    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    control = AsyncMock()
    control.refresh.return_value = ProxyControlSnapshot(
        allowed=True,
        status="enabled",
        reason_code="interactive",
        revision=1,
        source="database",
    )
    usage = RecordingUsageTracker()
    attempts = 0
    minted_proxies: list[str | None] = []

    class FlakyMinter:
        def __init__(self, _base_url, proxy=None):
            minted_proxies.append(proxy)

        async def mint(self):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise BlockedPageError("F5 challenge")
            return MintResult(cookies={"TSPD_101": "fresh"}, user_agent="fresh-UA")

    monkeypatch.setattr(pool_module, "CookieMinter", FlakyMinter)
    monkeypatch.setattr(pool_module, "OJVHttpAdapter", lambda *_args, **_kwargs: MagicMock())
    monkeypatch.setattr(pool_module, "OJVSession", FakeOJVSession)

    pool = APISessionPool(settings, proxy_control=control, proxy_usage=usage)
    pool._store = MemoryCookieStore()

    session = await pool.acquire()

    assert isinstance(session, FakeOJVSession)
    assert attempts == 3
    assert len(set(minted_proxies)) == 3
    assert usage.operations == ["mint", "mint", "mint"]


@pytest.mark.asyncio
async def test_challenged_stored_bundle_falls_back_to_fresh_on_demand_mint(
    monkeypatch, tmp_path,
):
    """A single persisted but challenged IP must not bypass fresh-IP retries."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool

    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    control = AsyncMock()
    control.refresh.return_value = ProxyControlSnapshot(
        allowed=True,
        status="enabled",
        reason_code="interactive",
        revision=1,
        source="database",
    )
    store = MemoryCookieStore()
    store.save_slot(0, {"TSPD_101": "stale"}, "stale-UA", "stale-token")
    initialized_cookies: list[dict] = []
    minted_proxies: list[str | None] = []

    class FreshMinter:
        def __init__(self, _base_url, proxy=None):
            minted_proxies.append(proxy)

        async def mint(self):
            return MintResult(cookies={"TSPD_101": "fresh"}, user_agent="fresh-UA")

    class CookieAwareSession(FakeOJVSession):
        async def initialize(self):
            cookies = self.adapter.cookies
            initialized_cookies.append(cookies)
            if cookies == {"TSPD_101": "stale"}:
                raise BlockedPageError("stored IP challenged")

    def fake_adapter(_settings, **kwargs):
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(pool_module, "CookieMinter", FreshMinter)
    monkeypatch.setattr(pool_module, "OJVHttpAdapter", fake_adapter)
    monkeypatch.setattr(pool_module, "OJVSession", CookieAwareSession)

    pool = APISessionPool(
        settings,
        proxy_control=control,
        proxy_usage=RecordingUsageTracker(),
    )
    pool._store = store

    session = await pool.acquire()

    assert isinstance(session, CookieAwareSession)
    assert initialized_cookies == [
        {"TSPD_101": "stale"},
        {"TSPD_101": "fresh"},
    ]
    assert len(minted_proxies) == 1
    assert store.slots["0"].cookies == {"TSPD_101": "fresh"}
    assert api_metrics.snapshot()["total_bundle_retries"] == 1


@pytest.mark.asyncio
async def test_on_demand_mint_does_not_start_more_ips_after_retry_budget(
    monkeypatch, tmp_path,
):
    """A failed slow mint must not keep spending after the request budget."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool

    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    control = AsyncMock()
    control.refresh.return_value = ProxyControlSnapshot(
        allowed=True,
        status="enabled",
        reason_code="interactive",
        revision=1,
        source="database",
    )
    attempts = 0

    class SlowBlockedMinter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def mint(self):
            nonlocal attempts
            attempts += 1
            raise BlockedPageError("slow challenge")

    monkeypatch.setattr(pool_module, "CookieMinter", SlowBlockedMinter)
    monkeypatch.setattr(pool_module, "_RETRY_BUDGET_S", -1)
    pool = APISessionPool(
        settings,
        proxy_control=control,
        proxy_usage=RecordingUsageTracker(),
    )
    pool._store = MemoryCookieStore()

    with pytest.raises(TimeoutError):
        await pool.acquire()

    assert attempts == 0


@pytest.mark.asyncio
async def test_waiting_for_mint_lock_does_not_renew_deadline(monkeypatch, tmp_path):
    """A concurrent request cannot spend after its original deadline expires."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool

    attempts = 0

    class ForbiddenLateMinter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def mint(self):
            nonlocal attempts
            attempts += 1
            return MintResult(cookies={"TSPD_101": "late"}, user_agent="late-UA")

    monkeypatch.setattr(pool_module, "CookieMinter", ForbiddenLateMinter)
    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    pool = APISessionPool(settings, allow_uncontrolled_proxy=True)
    pool._store = MemoryCookieStore()

    await pool._mint_lock.acquire()
    waiting = asyncio.create_task(
        pool._mint_on_demand(deadline=time.monotonic() + 0.01)
    )
    await asyncio.sleep(0.02)
    pool._mint_lock.release()

    with pytest.raises(TimeoutError):
        await waiting

    assert attempts == 0


@pytest.mark.asyncio
async def test_concurrent_empty_pool_mints_only_one_bundle(monkeypatch, tmp_path):
    """Two simultaneous user actions must share one paid mint."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool

    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    control = AsyncMock()
    control.refresh.return_value = ProxyControlSnapshot(
        allowed=True,
        status="enabled",
        reason_code="interactive",
        revision=1,
        source="database",
    )
    store = MemoryCookieStore()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    minted_proxies: list[str | None] = []

    class SlowMinter:
        def __init__(self, _base_url, proxy=None):
            minted_proxies.append(proxy)

        async def mint(self):
            first_started.set()
            await release_first.wait()
            return MintResult(cookies={"TSPD_101": "fresh"}, user_agent="fresh-UA")

    monkeypatch.setattr(pool_module, "CookieMinter", SlowMinter)
    monkeypatch.setattr(pool_module, "OJVHttpAdapter", lambda *_args, **_kwargs: MagicMock())
    monkeypatch.setattr(pool_module, "OJVSession", FakeOJVSession)

    pool = APISessionPool(
        settings,
        proxy_control=control,
        proxy_usage=RecordingUsageTracker(),
    )
    pool._store = store

    first = asyncio.create_task(pool.acquire())
    await first_started.wait()
    second = asyncio.create_task(pool.acquire())
    await asyncio.sleep(0)
    release_first.set()
    sessions = await asyncio.gather(first, second)

    assert all(isinstance(session, FakeOJVSession) for session in sessions)
    assert len(minted_proxies) == 1
    assert len(store.slots) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        ProxyBudgetExceededError("global"),
        ProxyUsagePersistenceError("ledger unavailable"),
    ],
)
async def test_cost_guard_fails_before_launching_on_demand_browser(
    monkeypatch, tmp_path, failure,
):
    """No reservation means no browser and therefore no paid proxy bytes."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool

    class DeniedTracker:
        @asynccontextmanager
        async def track(self, **_kwargs):
            raise failure
            yield

    minter_constructions = []

    class ForbiddenMinter:
        def __init__(self, *_args, **_kwargs):
            minter_constructions.append(True)

    monkeypatch.setattr(pool_module, "CookieMinter", ForbiddenMinter)
    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    control = AsyncMock()
    control.refresh.return_value = ProxyControlSnapshot(
        allowed=True,
        status="enabled",
        reason_code="interactive",
        revision=1,
        source="database",
    )
    pool = APISessionPool(settings, proxy_control=control, proxy_usage=DeniedTracker())
    pool._store = MemoryCookieStore()

    with pytest.raises(type(failure)):
        await pool.acquire()

    assert minter_constructions == []


@pytest.mark.asyncio
async def test_402_during_on_demand_mint_trips_persistent_control(monkeypatch, tmp_path):
    """Provider exhaustion must stop later traffic and remain an ops-only detail."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool

    class BillingMinter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def mint(self):
            raise httpx.ProxyError("402 Payment Required")

    monkeypatch.setattr(pool_module, "CookieMinter", BillingMinter)
    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    control = AsyncMock()
    control.refresh.return_value = ProxyControlSnapshot(
        allowed=True,
        status="enabled",
        reason_code="interactive",
        revision=1,
        source="database",
    )
    pool = APISessionPool(
        settings,
        proxy_control=control,
        proxy_usage=RecordingUsageTracker(),
    )
    pool._store = MemoryCookieStore()

    with pytest.raises(httpx.ProxyError):
        await pool.acquire()

    control.trip_billing_exhausted.assert_awaited_once()
