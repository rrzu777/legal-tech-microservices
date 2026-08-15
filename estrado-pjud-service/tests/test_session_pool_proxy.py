"""Tests para el pool de N slots con IP residencial sticky por slot.

Cubre: minteo con proxy_url distinto por slot, checkout de slots distintos
(G3), estrés de concurrencia, re-mint reactivo por-slot, cooldown (G6),
fallback sin proxy, y que un fallo de refresh no tumba acquire().

Mockea CookieMinter.mint, OJVHttpAdapter y OJVSession.initialize/close: nada
de browser/red real.
"""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.minter import MintResult
from app.cookie_store import CookieStoreLockTimeoutError
from app.failure_kind import MintUnavailableError
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError
from tests.helpers import cookie_values


def _make_config(pool_size=1, proxy_url=None, proxy_pool_size=3, block_pause_s=30):
    config = MagicMock()
    config.COOKIE_STORE_PATH = "/tmp/does-not-matter-proxy.json"
    config.PJUD_BASE_URL = "https://x"
    config.RATE_LIMIT_MS = 0
    config.SESSION_MAX_AGE_S = 1500
    config.POOL_SIZE = pool_size
    config.OJV_PROXY_URL = proxy_url
    config.OJV_PROXY_STICKY_LIFETIME = "1h"
    config.OJV_PROXY_POOL_SIZE = proxy_pool_size
    config.BLOCK_PAUSE_S = block_pause_s
    # Estos tests miden el comportamiento POR INTENTO de minteo; con reintentos
    # internos un mint fallido no propagaria y las aserciones perderian sentido.
    config.MINT_MAX_RETRIES = 1
    config.WORKER_SESSION_REUSE_VALIDATION_ENABLED = False
    config.SESSION_SOFT_VERIFY_AGE_S = 1200
    config.session_hard_effective_age_s = 3000
    return config


class _FakeSession:
    """Stand-in for OJVSession: no real adapter/browser behavior needed."""

    def __init__(self, adapter):
        self.adapter = adapter
        self._age = 0.0
        self.closed = False

    async def initialize(self):
        pass

    async def close(self):
        self.closed = True

    @property
    def age_seconds(self):
        return self._age


class _SnapshotAdapter:
    """Minimal adapter fake that preserves the pool's cookie snapshot contract."""

    def __init__(self, _settings, *, cookies=None, **_kwargs):
        self.cookies = cookie_values(cookies)

    def snapshot_cookies(self):
        return dict(self.cookies)


def _patch_pool_deps(monkeypatch, sp, mint_side_effect=None, patch_sleep=True):
    """Patch CookieMinter/OJVHttpAdapter/OJVSession/Settings/store with fakes.

    Captures every `proxy` kwarg passed to CookieMinter so tests can assert
    on distinctness. Returns the list of captured proxy urls (in mint order).

    By default also patches asyncio.sleep to a no-op so the real stagger
    (initialize) and cooldown (_mint_slot) delays don't slow down tests that
    aren't specifically exercising the timing (see test_cooldown_* which
    patches sleep itself with a clock-aware fake instead).
    """
    captured_proxies = []

    if patch_sleep:
        async def instant_sleep(_seconds):
            return None
        monkeypatch.setattr(sp.asyncio, "sleep", instant_sleep)

    class FakeMinter:
        def __init__(self, base_url, proxy=None):
            self.base_url = base_url
            self.proxy = proxy
            captured_proxies.append(proxy)

        async def mint(self):
            if mint_side_effect is not None:
                result = mint_side_effect(self.proxy)
                if isinstance(result, Exception):
                    raise result
                return result
            return MintResult(cookies={"TSPD_101": f"tok-for-{self.proxy}"}, user_agent="UA")

    monkeypatch.setattr(sp, "CookieMinter", FakeMinter)
    monkeypatch.setattr(sp, "Settings", lambda **k: MagicMock())
    monkeypatch.setattr(sp, "OJVHttpAdapter", _SnapshotAdapter)
    monkeypatch.setattr(sp, "OJVSession", _FakeSession)

    fake_store = MagicMock()
    fake_store.save_slot = MagicMock()
    monkeypatch.setattr(sp, "CookieStore", lambda path: fake_store)

    return captured_proxies, fake_store


@pytest.mark.asyncio
async def test_slot_mint_persists_cookie_jar_after_initialize(monkeypatch):
    """Saving browser cookies after OJV initialize would discard the refreshed jar."""
    from worker import session_pool as sp

    config = _make_config(proxy_url="http://proxy", proxy_pool_size=1)

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
            self.jar = cookie_values(cookies)

        def snapshot_cookies(self):
            return dict(self.jar)

    class SessionThatRenewsCookies(_FakeSession):
        async def initialize(self):
            self.adapter.jar = {"PHPSESSID": "renewed", "TS-current": "renewed-f5"}

    fake_store = MagicMock()
    monkeypatch.setattr(sp, "CookieMinter", FreshMinter)
    monkeypatch.setattr(sp, "Settings", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(sp, "OJVHttpAdapter", JarAdapter)
    monkeypatch.setattr(sp, "OJVSession", SessionThatRenewsCookies)
    monkeypatch.setattr(sp, "CookieStore", lambda _path: fake_store)

    pool = sp.SessionPool(config)
    await pool.initialize()

    saved_cookies = fake_store.save_slot.call_args.args[1]
    assert saved_cookies == {"PHPSESSID": "renewed", "TS-current": "renewed-f5"}


@pytest.mark.asyncio
async def test_worker_mint_persists_equivalent_cookie_scopes(monkeypatch):
    """A valid PJUD jar duplicated by scope must reach the worker slot store."""
    from worker import session_pool as sp

    config = _make_config(proxy_url="http://proxy", proxy_pool_size=1)

    class FreshMinter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def mint(self):
            return MintResult(cookies={"TS-current": "f5"}, user_agent="fresh-UA")

    class SessionWithEquivalentScopes(_FakeSession):
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

    fake_store = MagicMock()
    monkeypatch.setattr(sp, "CookieMinter", FreshMinter)
    monkeypatch.setattr(sp, "OJVSession", SessionWithEquivalentScopes)
    monkeypatch.setattr(sp, "CookieStore", lambda _path: fake_store)

    pool = sp.SessionPool(config)
    try:
        await pool.initialize()
        saved = fake_store.save_slot.call_args.args[1]
        assert {(cookie.name, cookie.domain, cookie.path) for cookie in saved} == {
            ("TS-current", "oficinajudicialvirtual.pjud.cl", "/"),
            ("PHPSESSID", "oficinajudicialvirtual.pjud.cl", "/"),
            ("PHPSESSID", ".pjud.cl", "/consultaUnificada.php"),
        }
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_worker_familia_slot_never_reads_api_on_demand_namespace(tmp_path):
    """The API candidate cannot overwrite cookies paired with worker slot 0's IP."""
    from app.cookie_store import CookieStore
    from app.session_pool import _API_COOKIE_STORE_SLOT
    from worker import session_pool as sp

    config = _make_config(proxy_url="http://worker-proxy", proxy_pool_size=1)
    config.COOKIE_STORE_PATH = str(tmp_path / "cookies.json")
    pool = sp.SessionPool(config)
    worker_proxy = "http://worker-slot-zero-proxy"
    pool._slots = [sp._Slot(index=0, proxy_url=worker_proxy, session=_FakeSession(MagicMock()))]

    store = CookieStore(config.COOKIE_STORE_PATH)
    store.save_slot(0, {"PHPSESSID": "worker-cookie"}, "worker-UA", "worker-token")
    store.save_slot(
        _API_COOKIE_STORE_SLOT,
        {"PHPSESSID": "api-cookie"},
        "api-UA",
        "api-token",
    )

    bundle, slot = await pool.acquire_familia_bundle()
    try:
        assert [(cookie.name, cookie.value) for cookie in bundle.cookies] == [
            ("PHPSESSID", "worker-cookie"),
        ]
        assert bundle.proxy_url == worker_proxy
    finally:
        await pool.release_familia_bundle(slot)


@pytest.mark.asyncio
async def test_slot_mint_failed_initialize_does_not_persist(monkeypatch):
    """A blocked slot candidate must leave its previous persisted bundle untouched."""
    from app.failure_kind import BlockedPageError
    from worker import session_pool as sp

    config = _make_config(proxy_url="http://proxy", proxy_pool_size=1)

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

    class BlockedSession(_FakeSession):
        async def initialize(self):
            raise BlockedPageError("challenge remains")

    old_cookies = {"PHPSESSID": "old", "TS-old": "old-f5"}

    class MemoryStore:
        def __init__(self):
            self.slots = {0: old_cookies}
            self.save_calls = []

        def save_slot(self, *args):
            self.save_calls.append(args)
            self.slots[args[0]] = args[1]

    fake_store = MemoryStore()
    monkeypatch.setattr(sp, "CookieMinter", FreshMinter)
    monkeypatch.setattr(sp, "Settings", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(sp, "OJVHttpAdapter", JarAdapter)
    monkeypatch.setattr(sp, "OJVSession", BlockedSession)
    monkeypatch.setattr(sp, "CookieStore", lambda _path: fake_store)

    pool = sp.SessionPool(config)

    with pytest.raises(BlockedPageError, match="challenge remains"):
        await pool.initialize()

    assert fake_store.save_calls == []
    assert fake_store.slots[0] == old_cookies


@pytest.mark.asyncio
async def test_slot_mint_ambiguous_cookie_snapshot_does_not_persist(monkeypatch):
    """An ambiguous jar closes its candidate and preserves the previous slot."""
    from worker import session_pool as sp

    config = _make_config(proxy_url="http://proxy", proxy_pool_size=1)
    old_cookies = {"PHPSESSID": "old", "TS-old": "old-f5"}

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

    created_sessions = []

    class InitializedSession(_FakeSession):
        def __init__(self, adapter):
            super().__init__(adapter)
            self.close_count = 0
            created_sessions.append(self)

        async def close(self):
            self.close_count += 1
            await super().close()

    class MemoryStore:
        def __init__(self):
            self.slots = {0: old_cookies}
            self.save_calls = []

        def save_slot(self, *args):
            self.save_calls.append(args)
            self.slots[args[0]] = args[1]

    fake_store = MemoryStore()
    monkeypatch.setattr(sp, "CookieMinter", FreshMinter)
    monkeypatch.setattr(sp, "Settings", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(sp, "OJVHttpAdapter", AmbiguousJarAdapter)
    monkeypatch.setattr(sp, "OJVSession", InitializedSession)
    monkeypatch.setattr(sp, "CookieStore", lambda _path: fake_store)

    pool = sp.SessionPool(config)
    old_session = _FakeSession(MagicMock())
    slot = sp._Slot(
        index=0,
        token="old-token",
        proxy_url="http://old-proxy",
        session=old_session,
        last_mint_ts=0,
    )

    with pytest.raises(ValueError, match="ambiguous_cookie_scope"):
        await pool._mint_slot(slot)

    assert len(created_sessions) == 1
    assert created_sessions[0].close_count == 1
    assert fake_store.save_calls == []
    assert fake_store.slots[0] == old_cookies
    assert (slot.token, slot.proxy_url, slot.session, slot.last_mint_ts) == (
        "old-token", "http://old-proxy", old_session, 0,
    )
    assert old_session.closed is False


@pytest.mark.asyncio
async def test_slot_mint_save_failure_closes_candidate_and_keeps_previous_slot(monkeypatch):
    """A persistence failure must not leak the initialized candidate or replace a usable slot."""
    from worker import session_pool as sp

    config = _make_config(proxy_url="http://proxy", proxy_pool_size=1)
    created_sessions = []

    class FreshMinter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def mint(self):
            return MintResult(cookies={"PHPSESSID": "minted"}, user_agent="fresh-UA")

    class SnapshotAdapter:
        def __init__(self, _settings, **_kwargs):
            pass

        def snapshot_cookies(self):
            return {"PHPSESSID": "initialized"}

    class InitializedSession(_FakeSession):
        def __init__(self, adapter):
            super().__init__(adapter)
            self.close_count = 0
            created_sessions.append(self)

        async def close(self):
            self.close_count += 1
            await super().close()

    class FailingStore:
        def save_slot(self, *_args):
            raise CookieStoreLockTimeoutError()

    monkeypatch.setattr(sp, "CookieMinter", FreshMinter)
    monkeypatch.setattr(sp, "Settings", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(sp, "OJVHttpAdapter", SnapshotAdapter)
    monkeypatch.setattr(sp, "OJVSession", InitializedSession)
    monkeypatch.setattr(sp, "CookieStore", lambda _path: FailingStore())

    pool = sp.SessionPool(config)
    old_session = _FakeSession(MagicMock())
    slot = sp._Slot(
        index=0,
        token="old-token",
        proxy_url="http://old-proxy",
        session=old_session,
        last_mint_ts=0,
    )

    with pytest.raises(CookieStoreLockTimeoutError):
        await pool._mint_slot(slot)

    assert len(created_sessions) == 1
    assert created_sessions[0].close_count == 1
    assert (slot.token, slot.proxy_url, slot.session, slot.last_mint_ts) == (
        "old-token", "http://old-proxy", old_session, 0,
    )
    assert old_session.closed is False


@pytest.mark.asyncio
async def test_mint_swap_ignores_retired_session_close_error(monkeypatch):
    """A durable mint remains installed when cleanup of the old adapter fails."""
    from worker import session_pool as sp

    config = _make_config(proxy_url="http://proxy", proxy_pool_size=1)
    captured, _ = _patch_pool_deps(monkeypatch, sp)
    pool = sp.SessionPool(config)

    class BrokenOldSession(_FakeSession):
        async def close(self):
            self.closed = True
            raise RuntimeError("old adapter close failed")

    old = BrokenOldSession(MagicMock())
    slot = sp._Slot(index=0, session=old, last_mint_ts=-10_000)

    await pool._mint_slot(slot, max_attempts=1)
    await pool.close_all()

    assert len(captured) == 1
    assert slot.session is not old
    assert old.closed is True


@pytest.mark.asyncio
async def test_mint_swap_does_not_wait_for_retired_session_close(monkeypatch):
    """A stuck old adapter cannot hold a new durable session past its caller deadline."""
    from worker import session_pool as sp

    config = _make_config(proxy_url="http://proxy", proxy_pool_size=1)
    captured, _ = _patch_pool_deps(monkeypatch, sp)
    monkeypatch.setattr(sp, "_CANDIDATE_CLOSE_TIMEOUT_S", 0.01)
    pool = sp.SessionPool(config)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingOldSession(_FakeSession):
        async def close(self):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    old = BlockingOldSession(MagicMock())
    slot = sp._Slot(index=0, session=old, last_mint_ts=-10_000)

    await asyncio.wait_for(
        pool._mint_slot(slot, max_attempts=1),
        timeout=0.03,
    )

    assert len(captured) == 1
    assert slot.session is not old
    await asyncio.wait_for(started.wait(), timeout=0.03)
    await asyncio.wait_for(cancelled.wait(), timeout=0.05)
    await pool.close_all()


@pytest.mark.asyncio
async def test_external_cancel_during_initialize_closes_worker_candidate_once(monkeypatch):
    """Worker cancellation must close its candidate before propagating it."""
    from worker import session_pool as sp

    started = asyncio.Event()
    created_sessions = []

    class FreshMinter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def mint(self):
            return MintResult(cookies={"PHPSESSID": "fresh"}, user_agent="fresh-UA")

    class BlockingSession(_FakeSession):
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

    config = _make_config(proxy_url="http://proxy", proxy_pool_size=1)
    monkeypatch.setattr(sp, "CookieMinter", FreshMinter)
    monkeypatch.setattr(sp, "Settings", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(sp, "OJVHttpAdapter", _SnapshotAdapter)
    monkeypatch.setattr(sp, "OJVSession", BlockingSession)
    pool = sp.SessionPool(config, proxy_usage=Tracker())

    task = asyncio.create_task(pool.initialize())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert created_sessions[0].close_count == 1
    assert pool._proxy_usage.exited is True


@pytest.mark.asyncio
async def test_402_during_mint_never_retries(monkeypatch):
    from worker import session_pool as sp

    config = _make_config(proxy_url="http://proxy", proxy_pool_size=1)
    config.MINT_MAX_RETRIES = 3
    captured, _ = _patch_pool_deps(
        monkeypatch,
        sp,
        mint_side_effect=lambda _proxy: httpx.ProxyError("402 Payment Required"),
    )
    pool = sp.SessionPool(config)

    with pytest.raises(httpx.ProxyError):
        await pool.initialize()

    assert len(captured) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    httpx.ConnectError("proxy transport unavailable"),
    MintUnavailableError("navigation_failed"),
])
async def test_worker_rotates_once_for_retryable_egress_failures(monkeypatch, failure):
    """Changing the shared predicate must stop the second sticky-IP attempt."""
    from worker import session_pool as sp

    config = _make_config(proxy_url="http://proxy", proxy_pool_size=1)
    config.MINT_MAX_RETRIES = 2
    calls = 0

    def mint_side_effect(_proxy):
        nonlocal calls
        calls += 1
        if calls == 1:
            return failure
        return MintResult(cookies={"TSPD_101": "fresh"}, user_agent="UA")

    captured, _ = _patch_pool_deps(monkeypatch, sp, mint_side_effect=mint_side_effect)
    pool = sp.SessionPool(config)

    await pool.initialize()

    assert len(captured) == 2
    assert captured[0] != captured[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    httpx.HTTPStatusError(
        "503", request=httpx.Request("GET", "https://ojv.test"),
        response=httpx.Response(503),
    ),
    ValueError("ambiguous_cookie_scope"),
])
async def test_worker_does_not_rotate_for_deterministic_or_pjud_failures(
    monkeypatch, failure,
):
    """Retrying a PJUD answer or cookie invariant would spend another IP pointlessly."""
    from worker import session_pool as sp

    config = _make_config(proxy_url="http://proxy", proxy_pool_size=1)
    config.MINT_MAX_RETRIES = 3
    captured, _ = _patch_pool_deps(
        monkeypatch, sp, mint_side_effect=lambda _proxy: failure,
    )
    pool = sp.SessionPool(config)

    with pytest.raises(type(failure)):
        await pool.initialize()

    assert len(captured) == 1


@pytest.mark.asyncio
async def test_worker_never_allocates_more_than_three_new_sticky_ips(monkeypatch):
    """A misconfigured retry count must not turn one mint into unbounded spend."""
    from worker import session_pool as sp

    config = _make_config(proxy_url="http://proxy", proxy_pool_size=1)
    config.MINT_MAX_RETRIES = 4
    captured, _ = _patch_pool_deps(
        monkeypatch,
        sp,
        mint_side_effect=lambda _proxy: httpx.ConnectError("proxy unavailable"),
    )
    pool = sp.SessionPool(config)

    with pytest.raises(httpx.ConnectError):
        await pool.initialize()

    assert len(captured) == 3


@pytest.mark.asyncio
async def test_worker_mint_deadline_cancels_traffic_and_finalizes_tracking(monkeypatch):
    """A deadline must cancel a paid mint before it can allocate a second IP."""
    from worker import session_pool as sp

    cancelled = asyncio.Event()
    cleanup_finished = asyncio.Event()
    captured: list[str | None] = []

    class BlockingMinter:
        def __init__(self, _base_url, proxy=None):
            captured.append(proxy)

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

    config = _make_config(proxy_url="http://proxy", proxy_pool_size=1)
    config.MINT_MAX_RETRIES = 3
    config.MINT_TRAFFIC_BUDGET_S = 0.02
    monkeypatch.setattr(sp, "CookieMinter", BlockingMinter)
    pool = sp.SessionPool(config, proxy_usage=Tracker())

    with pytest.raises(MintUnavailableError) as exc_info:
        await pool.initialize()

    assert exc_info.value.code == "deadline_exceeded"
    assert cancelled.is_set()
    assert cleanup_finished.is_set()
    assert len(captured) == 1
    assert pool._proxy_usage.exited is True


@pytest.mark.asyncio
async def test_worker_mint_deadline_also_cancels_session_initialize(monkeypatch):
    """The same bounded budget covers browser mint and OJV initialization."""
    from worker import session_pool as sp

    initialize_cancelled = asyncio.Event()
    created_sessions = []

    class FreshMinter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def mint(self):
            return MintResult(cookies={"PHPSESSID": "fresh"}, user_agent="fresh-UA")

    class BlockingSession(_FakeSession):
        def __init__(self, adapter):
            super().__init__(adapter)
            created_sessions.append(self)

        async def initialize(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                initialize_cancelled.set()
                raise

    config = _make_config(proxy_url="http://proxy", proxy_pool_size=1)
    config.MINT_TRAFFIC_BUDGET_S = 0.02
    monkeypatch.setattr(sp, "CookieMinter", FreshMinter)
    monkeypatch.setattr(sp, "Settings", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(sp, "OJVHttpAdapter", _SnapshotAdapter)
    monkeypatch.setattr(sp, "OJVSession", BlockingSession)
    pool = sp.SessionPool(config)

    with pytest.raises(MintUnavailableError) as exc_info:
        await pool.initialize()

    assert exc_info.value.code == "deadline_exceeded"
    assert initialize_cancelled.is_set()
    assert len(created_sessions) == 1
    assert created_sessions[0].closed is True


@pytest.mark.asyncio
async def test_billing_release_frees_slot_without_remint(monkeypatch):
    from worker import session_pool as sp

    config = _make_config(proxy_url="http://proxy", proxy_pool_size=1)
    captured, _ = _patch_pool_deps(monkeypatch, sp)
    pool = sp.SessionPool(config)
    await pool.initialize()
    session = await pool.acquire()

    await pool.release(session, healthy=False, remint=False)

    assert len(captured) == 1
    assert pool._sem._value == 1


@pytest.mark.asyncio
async def test_distinct_proxy_urls_per_slot(monkeypatch):
    """Each of the N slots mints through a DIFFERENT sticky proxy_url (token)."""
    from worker import session_pool as sp

    captured_proxies, _ = _patch_pool_deps(monkeypatch, sp)

    config = _make_config(proxy_url="http://user:pw@geo.iproyal.com:12321", proxy_pool_size=3)
    pool = sp.SessionPool(config)
    await pool.initialize()

    assert len(captured_proxies) == 3
    assert len(set(captured_proxies)) == 3, "expected 3 distinct proxy_urls, got duplicates"
    for p in captured_proxies:
        assert p is not None
        assert "_session-" in p


@pytest.mark.asyncio
async def test_proxy_mint_runs_inside_durable_usage_operation(monkeypatch):
    from worker import session_pool as sp

    _patch_pool_deps(monkeypatch, sp)
    tracked = []

    class Tracker:
        @asynccontextmanager
        async def track(self, **kwargs):
            tracked.append(kwargs)
            yield SimpleNamespace(retry_count=0)

    pool = sp.SessionPool(
        _make_config(proxy_url="http://proxy", proxy_pool_size=1),
        proxy_usage=Tracker(),
    )
    await pool.initialize()

    assert len(tracked) == 1
    assert tracked[0]["operation"] == "mint"


@pytest.mark.asyncio
async def test_budget_denied_mint_never_retries_or_launches_browser(monkeypatch):
    from worker import session_pool as sp

    captured, _ = _patch_pool_deps(monkeypatch, sp)

    class DeniedTracker:
        @asynccontextmanager
        async def track(self, **_kwargs):
            raise ProxyBudgetExceededError("blocked")
            yield

    config = _make_config(proxy_url="http://proxy", proxy_pool_size=1)
    config.MINT_MAX_RETRIES = 3
    pool = sp.SessionPool(config, proxy_usage=DeniedTracker())

    with pytest.raises(ProxyBudgetExceededError):
        await pool.initialize()

    assert captured == []
    assert pool.mint_attempts == 1


@pytest.mark.asyncio
async def test_expired_refresh_telemetry_failure_pauses_and_does_not_checkout(monkeypatch):
    from worker import session_pool as sp

    _patch_pool_deps(monkeypatch, sp)
    control = AsyncMock()
    pool = sp.SessionPool(
        _make_config(proxy_url="http://proxy", proxy_pool_size=1),
        proxy_control=control,
    )
    await pool.initialize()
    pool._slots[0].session._age = 9_999
    pool._refresh_slot = AsyncMock(
        side_effect=ProxyUsagePersistenceError("ledger unavailable"),
    )

    with pytest.raises(ProxyUsagePersistenceError):
        await pool.acquire()

    control.pause_telemetry_unavailable.assert_awaited_once()
    assert pool._slots[0].busy is False
    assert pool._sem._value == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,control_method", [
    (ProxyUsagePersistenceError("ledger unavailable"), "pause_telemetry_unavailable"),
    (httpx.ProxyError("402 Payment Required"), "trip_billing_exhausted"),
])
async def test_reactive_remint_cost_failure_trips_control_and_frees_slot(
    monkeypatch, failure, control_method,
):
    from worker import session_pool as sp

    _patch_pool_deps(monkeypatch, sp)
    control = AsyncMock()
    pool = sp.SessionPool(
        _make_config(proxy_url="http://proxy", proxy_pool_size=1),
        proxy_control=control,
    )
    await pool.initialize()
    session = await pool.acquire()
    pool._refresh_slot = AsyncMock(side_effect=failure)

    with pytest.raises(type(failure)):
        await pool.release(session, healthy=False)

    getattr(control, control_method).assert_awaited_once()
    assert pool._slots[0].busy is False
    assert pool._sem._value == 1


@pytest.mark.asyncio
async def test_no_proxy_fallback_mints_without_proxy(monkeypatch):
    """OJV_PROXY_URL=None => legacy behavior: proxy=None, N=POOL_SIZE."""
    from worker import session_pool as sp

    captured_proxies, fake_store = _patch_pool_deps(monkeypatch, sp)

    config = _make_config(pool_size=2, proxy_url=None)
    pool = sp.SessionPool(config)
    await pool.initialize()

    assert captured_proxies == [None, None]
    for call in fake_store.save_slot.call_args_list:
        _, kwargs = call
        args = call.args
        # save_slot(slot_id, cookies, user_agent, proxy_token) — token is last
        proxy_token_arg = kwargs.get("proxy_token", args[-1] if args else None)
        assert proxy_token_arg is None


@pytest.mark.asyncio
async def test_checkout_returns_distinct_slots(monkeypatch):
    """Two acquire() calls without release must return DIFFERENT slot sessions (G3)."""
    from worker import session_pool as sp

    _patch_pool_deps(monkeypatch, sp)
    config = _make_config(proxy_url="http://user:pw@geo.iproyal.com:12321", proxy_pool_size=3)
    pool = sp.SessionPool(config)
    await pool.initialize()

    s1 = await pool.acquire()
    s2 = await pool.acquire()
    assert s1 is not s2

    # A 4th... well with N=3, a 3rd acquire should still succeed (3rd distinct slot).
    s3 = await pool.acquire()
    assert s3 is not s1 and s3 is not s2

    # Now the pool is exhausted (N=3, 3 in use): a 4th acquire must BLOCK.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(pool.acquire(), timeout=0.2)

    # Release one, and the 4th acquire should now proceed.
    await pool.release(s1)
    s4 = await asyncio.wait_for(pool.acquire(), timeout=0.5)
    assert s4 is s1


@pytest.mark.asyncio
async def test_concurrency_stress_no_double_checkout(monkeypatch):
    """Many coroutines doing acquire -> await -> release never share a slot."""
    from worker import session_pool as sp

    _patch_pool_deps(monkeypatch, sp)
    config = _make_config(proxy_url="http://user:pw@geo.iproyal.com:12321", proxy_pool_size=3)
    pool = sp.SessionPool(config)
    await pool.initialize()

    in_use: set = set()
    max_concurrent = 0
    lock = asyncio.Lock()
    errors = []

    async def worker_task(i):
        nonlocal max_concurrent
        try:
            session = await pool.acquire()
            async with lock:
                if id(session) in in_use:
                    errors.append(f"slot {id(session)} double-checked-out (task {i})")
                in_use.add(id(session))
                max_concurrent = max(max_concurrent, len(in_use))
            await asyncio.sleep(0.01)
            async with lock:
                in_use.discard(id(session))
            await pool.release(session)
        except Exception as e:  # pragma: no cover - surfaced via errors list
            errors.append(str(e))

    await asyncio.gather(*(worker_task(i) for i in range(20)))

    assert errors == []
    assert max_concurrent <= 3


@pytest.mark.asyncio
async def test_release_unhealthy_remints_only_that_slot(monkeypatch):
    """release(session, healthy=False) re-mints ONLY that slot; others untouched."""
    from worker import session_pool as sp

    _patch_pool_deps(monkeypatch, sp)
    config = _make_config(proxy_url="http://user:pw@geo.iproyal.com:12321", proxy_pool_size=3)
    pool = sp.SessionPool(config)
    await pool.initialize()

    proxies_before = [slot.proxy_url for slot in pool._slots]
    sessions_before = [slot.session for slot in pool._slots]

    target_session = await pool.acquire()
    target_idx = next(i for i, s in enumerate(sessions_before) if s is target_session)

    await pool.release(target_session, healthy=False)

    proxies_after = [slot.proxy_url for slot in pool._slots]
    sessions_after = [slot.session for slot in pool._slots]

    for i in range(3):
        if i == target_idx:
            assert proxies_after[i] != proxies_before[i]
            assert sessions_after[i] is not sessions_before[i]
        else:
            assert proxies_after[i] == proxies_before[i]
            assert sessions_after[i] is sessions_before[i]


@pytest.mark.asyncio
async def test_cooldown_spaces_reremints_by_block_pause(monkeypatch):
    """Two quick re-mints of the same slot are spaced by >= BLOCK_PAUSE_S (G6)."""
    from worker import session_pool as sp

    _patch_pool_deps(monkeypatch, sp, patch_sleep=False)
    config = _make_config(proxy_url="http://user:pw@geo.iproyal.com:12321", proxy_pool_size=1, block_pause_s=30)
    pool = sp.SessionPool(config)

    # Fake monotonic clock we control.
    fake_time = {"t": 1000.0}

    def fake_monotonic():
        return fake_time["t"]

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        # Don't actually sleep in the test; just advance the fake clock.
        fake_time["t"] += seconds

    monkeypatch.setattr(sp.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(sp.asyncio, "sleep", fake_sleep)

    await pool.initialize()  # first mint: no cooldown wait expected
    assert sleep_calls == []  # stagger sleep only applies between slots (N=1 here -> none)

    slot = pool._slots[0]
    # Simulate only 5s elapsed since last mint -> re-mint should wait ~25s.
    fake_time["t"] += 5
    await pool._refresh_slot(slot)

    assert len(sleep_calls) == 1
    assert sleep_calls[0] == pytest.approx(25.0)


@pytest.mark.asyncio
async def test_refresh_failure_during_acquire_is_non_fatal(monkeypatch):
    """If _mint_slot raises during an acquire-triggered refresh, acquire still
    returns the existing (stale) session instead of raising."""
    from worker import session_pool as sp

    call_count = {"n": 0}

    def mint_side_effect(proxy):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return MintResult(cookies={"TSPD_101": "a"}, user_agent="UA")
        raise RuntimeError("mint failed")

    _patch_pool_deps(monkeypatch, sp, mint_side_effect=mint_side_effect)
    config = _make_config(proxy_url="http://user:pw@geo.iproyal.com:12321", proxy_pool_size=1)
    pool = sp.SessionPool(config)
    await pool.initialize()

    # Force the existing session to look expired so acquire() tries to refresh.
    pool._slots[0].session._age = config.SESSION_MAX_AGE_S + 1

    stale_session = pool._slots[0].session
    result = await pool.acquire()
    assert result is stale_session


@pytest.mark.asyncio
async def test_release_of_unacquired_session_does_not_over_release(monkeypatch):
    """release() of a session that was never acquired must NOT release the
    semaphore (C1) and must not raise."""
    from worker import session_pool as sp

    _patch_pool_deps(monkeypatch, sp)
    config = _make_config(proxy_url="http://user:pw@geo.iproyal.com:12321", proxy_pool_size=3)
    pool = sp.SessionPool(config)
    await pool.initialize()

    sem_value_before = pool._sem._value
    fake_session = object()  # never acquired / not registered

    await pool.release(fake_session)  # must not raise

    assert pool._sem._value == sem_value_before  # no over-release

    # Sanity: the pool still admits exactly N and not N+1.
    acquired = [await pool.acquire() for _ in range(3)]
    assert len(acquired) == 3
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(pool.acquire(), timeout=0.2)


@pytest.mark.asyncio
async def test_release_unhealthy_frees_slot_and_full_round_works(monkeypatch):
    """acquire -> release(healthy=False) re-mints the slot and frees it (no
    stuck-busy); a subsequent full round of N acquires works (no StopIteration,
    no semaphore drift)."""
    from worker import session_pool as sp

    _patch_pool_deps(monkeypatch, sp)
    config = _make_config(proxy_url="http://user:pw@geo.iproyal.com:12321", proxy_pool_size=3)
    pool = sp.SessionPool(config)
    await pool.initialize()

    s = await pool.acquire()
    slot = pool._checkout[s]
    proxy_before = slot.proxy_url
    session_before = slot.session

    await pool.release(s, healthy=False)

    # Slot re-minted and freed.
    assert slot.busy is False
    assert slot.session is not session_before
    assert slot.proxy_url != proxy_before
    assert s not in pool._checkout  # checkout registry cleaned

    # Full round of N acquires works and returns N distinct slots.
    acquired = [await pool.acquire() for _ in range(3)]
    slots_used = {id(pool._checkout[a]) for a in acquired}
    assert len(slots_used) == 3
    for a in acquired:
        await pool.release(a)
    assert pool._checkout == {}  # all released
