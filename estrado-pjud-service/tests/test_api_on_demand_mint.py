import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import Settings
from app.cookie_store import CookieBundle, CookieStoreLockTimeoutError
from app.minter import MintResult
from app.failure_kind import (
    BlockedPageError,
    MintUnavailableError,
    NoUsableBundleError,
    PoolUnavailableError,
    is_expected_acquisition_failure,
)
from app.metrics import api_metrics
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError
from tests.helpers import FakeOJVSession
from worker.proxy_control import ProxyControlSnapshot


class MemoryCookieStore:
    def __init__(self):
        self.slots: dict[str, CookieBundle] = {}
        self.save_calls: list[tuple] = []

    def load_all(self):
        return dict(self.slots)

    def save_slot(self, slot_id, cookies, user_agent, proxy_token):
        self.save_calls.append((slot_id, cookies, user_agent, proxy_token))
        self.slots[str(slot_id)] = CookieBundle(
            cookies=cookies,
            user_agent=user_agent,
            saved_at=time.time(),
            proxy_token=proxy_token,
        )


class SnapshotAdapter:
    """Adapter fake that keeps the cookie jar contract used by the pools."""

    def __init__(self, _settings, *, cookies=None, **_kwargs):
        self.cookies = dict(cookies or {})

    def snapshot_cookies(self):
        return dict(self.cookies)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (MintUnavailableError("navigation_failed"), "upstream_unavailable"),
        (MintUnavailableError("deadline_exceeded"), "deadline_exceeded"),
        (NoUsableBundleError(), "mint_exhausted"),
        (BlockedPageError("F5 response sentinel"), "session_blocked"),
        (httpx.ProxyError("proxy credential sentinel"), "proxy_transport"),
        (TimeoutError("deadline sentinel"), "deadline_exceeded"),
    ],
)
async def test_acquire_wraps_only_exhausted_operational_failures(monkeypatch, tmp_path, failure, expected_code):
    """Removing the API acquisition boundary would leak raw operational failures."""
    from app.session_pool import APISessionPool, _API_COOKIE_STORE_SLOT

    settings = Settings(
        API_KEY="t",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    pool = APISessionPool(settings, allow_uncontrolled_proxy=True)
    pool._acquire_with_deadline = AsyncMock(side_effect=failure)

    with pytest.raises(PoolUnavailableError) as exc_info:
        await pool.acquire()

    assert exc_info.value.code == expected_code
    assert str(exc_info.value) == expected_code
    assert is_expected_acquisition_failure(failure) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        ValueError("parser invariant sentinel"),
        AssertionError("assertion invariant sentinel"),
        httpx.ProxyError("402 Payment Required"),
        ProxyBudgetExceededError("global"),
        ProxyUsagePersistenceError("telemetry sentinel"),
    ],
)
async def test_acquire_preserves_non_operational_failure_identity(monkeypatch, tmp_path, failure):
    """Wrapping all exceptions would mask billing, telemetry and programming defects."""
    from app.session_pool import APISessionPool, ProxyTrafficDisabledError

    settings = Settings(
        API_KEY="t",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    pool = APISessionPool(settings, allow_uncontrolled_proxy=True)
    pool._acquire_with_deadline = AsyncMock(side_effect=failure)

    with pytest.raises(type(failure)) as exc_info:
        await pool.acquire()

    assert exc_info.value is failure
    assert is_expected_acquisition_failure(failure) is False

    control_failure = ProxyTrafficDisabledError("control disabled")
    pool._acquire_with_deadline = AsyncMock(side_effect=control_failure)
    with pytest.raises(ProxyTrafficDisabledError) as control_exc_info:
        await pool.acquire()

    assert control_exc_info.value is control_failure
    assert is_expected_acquisition_failure(control_failure) is False


@pytest.mark.asyncio
async def test_on_demand_mint_persists_cookie_jar_after_initialize(monkeypatch, tmp_path):
    """Persisting Playwright cookies after OJV refresh would lose renewed session state."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool, _API_COOKIE_STORE_SLOT

    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    store = MemoryCookieStore()
    store.save_slot(0, {"PHPSESSID": "old", "TS-old": "old-f5"}, "old-UA", "old-token")

    class FreshMinter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def mint(self):
            return MintResult(
                cookies={"PHPSESSID": "minted", "TS-minted": "minted-f5"},
                user_agent="fresh-UA",
            )

    class JarAdapter:
        def __init__(self, _settings, *, cookies=None, **_kwargs):
            self.jar = dict(cookies or {})

        def snapshot_cookies(self):
            return dict(self.jar)

    class SessionThatRenewsCookies(FakeOJVSession):
        async def initialize(self):
            self.adapter.jar = {"PHPSESSID": "renewed", "TS-current": "renewed-f5"}

    monkeypatch.setattr(pool_module, "CookieMinter", FreshMinter)
    monkeypatch.setattr(pool_module, "OJVHttpAdapter", JarAdapter)
    monkeypatch.setattr(pool_module, "OJVSession", SessionThatRenewsCookies)

    pool = APISessionPool(settings, allow_uncontrolled_proxy=True)
    pool._store = store

    await pool._mint_new_bundle()

    assert store.slots["0"].cookies == {"PHPSESSID": "old", "TS-old": "old-f5"}
    assert store.slots[_API_COOKIE_STORE_SLOT].cookies == {
        "PHPSESSID": "renewed", "TS-current": "renewed-f5",
    }


@pytest.mark.asyncio
async def test_on_demand_mint_persists_equivalent_cookie_scopes(monkeypatch, tmp_path):
    """A valid PJUD jar duplicated by scope must reach the API bundle store."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool, _API_COOKIE_STORE_SLOT

    class FreshMinter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def mint(self):
            return MintResult(cookies={"TS-current": "f5"}, user_agent="fresh-UA")

    class SessionWithEquivalentScopes(FakeOJVSession):
        async def initialize(self):
            self.adapter.cookies.set(
                "PHPSESSID", "renewed", domain="oficinajudicialvirtual.pjud.cl", path="/",
            )
            self.adapter.cookies.set(
                "PHPSESSID", "renewed", domain=".pjud.cl", path="/consultaUnificada.php",
            )

        async def close(self):
            await self.adapter.close()
            await super().close()

    monkeypatch.setattr(pool_module, "CookieMinter", FreshMinter)
    monkeypatch.setattr(pool_module, "OJVSession", SessionWithEquivalentScopes)
    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    store = MemoryCookieStore()
    pool = APISessionPool(settings, allow_uncontrolled_proxy=True)
    pool._store = store

    session = await pool._mint_new_bundle()
    try:
        assert store.slots[_API_COOKIE_STORE_SLOT].cookies == {
            "TS-current": "f5",
            "PHPSESSID": "renewed",
        }
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_on_demand_store_lock_timeout_closes_candidate_without_a_second_ip(monkeypatch, tmp_path):
    """A local persistence conflict must not spend a second proxy token."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool, _API_COOKIE_STORE_SLOT

    minted_proxies: list[str | None] = []
    created_sessions = []

    class FreshMinter:
        def __init__(self, _base_url, proxy=None):
            minted_proxies.append(proxy)

        async def mint(self):
            return MintResult(cookies={"PHPSESSID": "fresh"}, user_agent="fresh-UA")

    class ClosableSession(FakeOJVSession):
        def __init__(self, adapter):
            super().__init__(adapter)
            self.close_count = 0
            created_sessions.append(self)

        async def close(self):
            self.close_count += 1

    class LockedStore(MemoryCookieStore):
        def save_slot(self, *_args):
            raise CookieStoreLockTimeoutError()

    monkeypatch.setattr(pool_module, "CookieMinter", FreshMinter)
    monkeypatch.setattr(pool_module, "OJVHttpAdapter", SnapshotAdapter)
    monkeypatch.setattr(pool_module, "OJVSession", ClosableSession)
    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    pool = APISessionPool(settings, allow_uncontrolled_proxy=True)
    pool._store = LockedStore()

    with pytest.raises(CookieStoreLockTimeoutError):
        await pool._mint_on_demand()

    assert len(minted_proxies) == 1
    assert created_sessions[0].close_count == 1


@pytest.mark.asyncio
async def test_external_cancel_during_initialize_closes_api_candidate_once(monkeypatch, tmp_path):
    """Disconnecting a request cannot leave its initialized HTTP adapter alive."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool

    started = asyncio.Event()
    created_sessions = []

    class FreshMinter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def mint(self):
            return MintResult(cookies={"PHPSESSID": "fresh"}, user_agent="fresh-UA")

    class BlockingSession(FakeOJVSession):
        def __init__(self, adapter):
            super().__init__(adapter)
            self.close_count = 0
            created_sessions.append(self)

        async def initialize(self):
            started.set()
            await asyncio.Event().wait()

        async def close(self):
            self.close_count += 1

    class Tracker:
        exited = False

        @asynccontextmanager
        async def track(self, **_kwargs):
            try:
                yield SimpleNamespace(retry_count=0)
            finally:
                self.exited = True

    monkeypatch.setattr(pool_module, "CookieMinter", FreshMinter)
    monkeypatch.setattr(pool_module, "OJVHttpAdapter", SnapshotAdapter)
    monkeypatch.setattr(pool_module, "OJVSession", BlockingSession)
    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    tracker = Tracker()
    pool = APISessionPool(
        settings,
        allow_uncontrolled_proxy=True,
        proxy_usage=tracker,
    )
    pool._store = MemoryCookieStore()

    task = asyncio.create_task(pool._mint_new_bundle())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert created_sessions[0].close_count == 1
    assert tracker.exited is True


@pytest.mark.asyncio
async def test_external_cancel_during_stored_bundle_initialize_closes_session_once(
    monkeypatch, tmp_path,
):
    """The same cleanup applies before a persisted bundle can enter the API pool."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool

    started = asyncio.Event()
    created_sessions = []

    class BlockingSession(FakeOJVSession):
        def __init__(self, adapter):
            super().__init__(adapter)
            self.close_count = 0
            created_sessions.append(self)

        async def initialize(self):
            started.set()
            await asyncio.Event().wait()

        async def close(self):
            self.close_count += 1

    monkeypatch.setattr(pool_module, "OJVHttpAdapter", SnapshotAdapter)
    monkeypatch.setattr(pool_module, "OJVSession", BlockingSession)
    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    store = MemoryCookieStore()
    store.save_slot(0, {"PHPSESSID": "stored"}, "stored-UA", "stored-token")
    pool = APISessionPool(settings, allow_uncontrolled_proxy=True)
    pool._store = store

    task = asyncio.create_task(pool.acquire())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert created_sessions[0].close_count == 1


@pytest.mark.asyncio
async def test_on_demand_failed_initialize_keeps_existing_bundle(monkeypatch, tmp_path):
    """A blocked refreshed session must not overwrite the last known bundle."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool

    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    store = MemoryCookieStore()
    old_cookies = {"PHPSESSID": "old", "TS-old": "old-f5"}
    store.slots["0"] = CookieBundle(
        cookies=old_cookies,
        user_agent="old-UA",
        saved_at=time.time(),
        proxy_token="old-token",
    )

    class FreshMinter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def mint(self):
            return MintResult(cookies={"PHPSESSID": "minted"}, user_agent="fresh-UA")

    class JarAdapter:
        def __init__(self, _settings, **_kwargs):
            pass

        def snapshot_cookies(self):
            return {"PHPSESSID": "renewed", "TS-current": "renewed-f5"}

    class BlockedSession(FakeOJVSession):
        async def initialize(self):
            raise BlockedPageError("challenge remains")

    monkeypatch.setattr(pool_module, "CookieMinter", FreshMinter)
    monkeypatch.setattr(pool_module, "OJVHttpAdapter", JarAdapter)
    monkeypatch.setattr(pool_module, "OJVSession", BlockedSession)

    pool = APISessionPool(settings, allow_uncontrolled_proxy=True)
    pool._store = store

    with pytest.raises(BlockedPageError, match="challenge remains"):
        await pool._mint_new_bundle()

    assert store.save_calls == []
    assert store.slots["0"].cookies == old_cookies


@pytest.mark.asyncio
async def test_on_demand_ambiguous_cookie_snapshot_keeps_existing_bundle(monkeypatch, tmp_path):
    """An ambiguous jar must fail before it can overwrite a persisted bundle."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool

    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    store = MemoryCookieStore()
    old_cookies = {"PHPSESSID": "old", "TS-old": "old-f5"}
    store.slots["0"] = CookieBundle(
        cookies=old_cookies,
        user_agent="old-UA",
        saved_at=time.time(),
        proxy_token="old-token",
    )

    class FreshMinter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def mint(self):
            return MintResult(cookies={"PHPSESSID": "minted"}, user_agent="fresh-UA")

    class AmbiguousJarAdapter:
        def __init__(self, _settings, **_kwargs):
            pass

        def snapshot_cookies(self):
            raise ValueError("ambiguous_cookie_scope")

    monkeypatch.setattr(pool_module, "CookieMinter", FreshMinter)
    monkeypatch.setattr(pool_module, "OJVHttpAdapter", AmbiguousJarAdapter)
    monkeypatch.setattr(pool_module, "OJVSession", FakeOJVSession)

    pool = APISessionPool(settings, allow_uncontrolled_proxy=True)
    pool._store = store

    with pytest.raises(ValueError, match="ambiguous_cookie_scope"):
        await pool._mint_new_bundle()

    assert store.save_calls == []
    assert store.slots["0"].cookies == old_cookies


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
    monkeypatch.setattr(pool_module, "OJVHttpAdapter", SnapshotAdapter)
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
        return SnapshotAdapter(_settings, **kwargs)

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
    monkeypatch.setattr(pool_module, "OJVHttpAdapter", SnapshotAdapter)
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
    from app.session_pool import APISessionPool, _API_COOKIE_STORE_SLOT

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
        return SnapshotAdapter(_settings, **kwargs)

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
    assert store.slots["0"].cookies == {"TSPD_101": "stale"}
    assert store.slots[_API_COOKIE_STORE_SLOT].cookies == {"TSPD_101": "fresh"}
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

    with pytest.raises(PoolUnavailableError) as exc_info:
        await pool.acquire()

    assert exc_info.value.code == "deadline_exceeded"
    assert attempts == 0


@pytest.mark.asyncio
async def test_on_demand_mint_deadline_cancels_paid_traffic_and_finalizes_tracking(
    monkeypatch, tmp_path,
):
    """Removing the in-flight deadline would let a timed-out request keep spending."""
    from app import session_pool as pool_module
    from app.session_pool import APISessionPool

    cancelled = asyncio.Event()
    cleanup_finished = asyncio.Event()
    minted_proxies: list[str | None] = []

    class BlockingMinter:
        def __init__(self, _base_url, proxy=None):
            minted_proxies.append(proxy)

        async def mint(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            finally:
                cleanup_finished.set()

    class Tracker:
        def __init__(self):
            self.exited = False

        @asynccontextmanager
        async def track(self, **_kwargs):
            try:
                yield SimpleNamespace(retry_count=0)
            finally:
                self.exited = True

    monkeypatch.setattr(pool_module, "CookieMinter", BlockingMinter)
    monkeypatch.setattr(pool_module, "_RETRY_BUDGET_S", 0.02)
    tracker = Tracker()
    settings = Settings(
        API_KEY="t",
        OJV_PROXY_URL="http://user:password@geo.iproyal.com:12321",
        COOKIE_STORE_PATH=str(tmp_path / "cookies.json"),
        _env_file=None,
    )
    pool = APISessionPool(
        settings,
        allow_uncontrolled_proxy=True,
        proxy_usage=tracker,
    )
    pool._store = MemoryCookieStore()

    with pytest.raises(PoolUnavailableError) as exc_info:
        await pool.acquire()

    assert exc_info.value.code == "deadline_exceeded"
    assert cancelled.is_set()
    assert cleanup_finished.is_set()
    assert len(minted_proxies) == 1
    assert tracker.exited is True


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
    monkeypatch.setattr(pool_module, "OJVHttpAdapter", SnapshotAdapter)
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
