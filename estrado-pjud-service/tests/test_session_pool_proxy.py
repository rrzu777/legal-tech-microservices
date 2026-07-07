"""Tests para el pool de N slots con IP residencial sticky por slot.

Cubre: minteo con proxy_url distinto por slot, checkout de slots distintos
(G3), estrés de concurrencia, re-mint reactivo por-slot, cooldown (G6),
fallback sin proxy, y que un fallo de refresh no tumba acquire().

Mockea CookieMinter.mint, OJVHttpAdapter y OJVSession.initialize/close: nada
de browser/red real.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.minter import MintResult


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
    monkeypatch.setattr(sp, "OJVHttpAdapter", lambda *a, **k: MagicMock(kwargs=k))
    monkeypatch.setattr(sp, "OJVSession", _FakeSession)

    fake_store = MagicMock()
    fake_store.save_slot = MagicMock()
    monkeypatch.setattr(sp, "CookieStore", lambda path: fake_store)

    return captured_proxies, fake_store


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
        # save_slot(slot_id, cookies, user_agent, proxy_url) — proxy_url is last
        proxy_url_arg = kwargs.get("proxy_url", args[-1] if args else None)
        assert proxy_url_arg is None


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
