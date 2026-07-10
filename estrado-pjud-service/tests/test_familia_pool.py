# tests/test_familia_pool.py
"""Checkout de bundle F5 para el path Familia: presta el bundle de un slot
sin tomar la guest OJVSession, con la misma semántica de re-mint reactivo."""
from unittest.mock import MagicMock

import pytest

from app.cookie_store import CookieBundle
from app.minter import MintResult


def _make_config(proxy_url="http://user:pw@geo.iproyal.com:12321", proxy_pool_size=3):
    config = MagicMock()
    config.COOKIE_STORE_PATH = "/tmp/does-not-matter-familia.json"
    config.PJUD_BASE_URL = "https://x"
    config.RATE_LIMIT_MS = 0
    config.SESSION_MAX_AGE_S = 1500
    config.POOL_SIZE = 1
    config.OJV_PROXY_URL = proxy_url
    config.OJV_PROXY_STICKY_LIFETIME = "1h"
    config.OJV_PROXY_POOL_SIZE = proxy_pool_size
    config.BLOCK_PAUSE_S = 30
    return config


class _FakeSession:
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


def _patch(monkeypatch, sp):
    async def instant_sleep(_s):
        return None
    monkeypatch.setattr(sp.asyncio, "sleep", instant_sleep)

    class FakeMinter:
        def __init__(self, base_url, proxy=None):
            self.proxy = proxy

        async def mint(self):
            return MintResult(cookies={"TSPD_101": f"tok-{self.proxy}"}, user_agent="UA")

    monkeypatch.setattr(sp, "CookieMinter", FakeMinter)
    monkeypatch.setattr(sp, "Settings", lambda **k: MagicMock())
    monkeypatch.setattr(sp, "OJVHttpAdapter", lambda *a, **k: MagicMock())
    monkeypatch.setattr(sp, "OJVSession", _FakeSession)

    store = MagicMock()
    store.save_slot = MagicMock()
    store.load_slot = MagicMock(
        return_value=CookieBundle(
            cookies={"TSPD_101": "x"}, user_agent="UA", saved_at=0.0,
            proxy_url="http://user:pw@geo.iproyal.com:12321",
        )
    )
    monkeypatch.setattr(sp, "CookieStore", lambda path: store)
    return store


@pytest.mark.asyncio
async def test_acquire_familia_bundle_returns_bundle_and_slot(monkeypatch):
    from worker import session_pool as sp

    _patch(monkeypatch, sp)
    pool = sp.SessionPool(_make_config(proxy_pool_size=1))
    await pool.initialize()

    bundle, slot = await pool.acquire_familia_bundle()
    assert bundle.cookies == {"TSPD_101": "x"}
    assert bundle.user_agent == "UA"
    assert slot.busy is True  # slot tomado, nadie más lo usa
    await pool.release_familia_bundle(slot, healthy=True)
    assert slot.busy is False


@pytest.mark.asyncio
async def test_familia_checkout_respects_semaphore(monkeypatch):
    import asyncio
    from worker import session_pool as sp

    _patch(monkeypatch, sp)
    pool = sp.SessionPool(_make_config(proxy_pool_size=1))
    await pool.initialize()

    _, slot = await pool.acquire_familia_bundle()
    # N=1 y el único slot está tomado → un segundo checkout debe bloquear.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(pool.acquire_familia_bundle(), timeout=0.2)
    await pool.release_familia_bundle(slot, healthy=True)
    # Tras liberar, procede.
    _, slot2 = await asyncio.wait_for(pool.acquire_familia_bundle(), timeout=0.5)
    assert slot2 is slot


@pytest.mark.asyncio
async def test_release_unhealthy_remints_the_slot(monkeypatch):
    from worker import session_pool as sp

    _patch(monkeypatch, sp)
    pool = sp.SessionPool(_make_config(proxy_pool_size=1))
    await pool.initialize()

    _, slot = await pool.acquire_familia_bundle()
    proxy_before = slot.proxy_url
    session_before = slot.session

    await pool.release_familia_bundle(slot, healthy=False)

    assert slot.busy is False
    assert slot.proxy_url != proxy_before  # IP nueva
    assert slot.session is not session_before


@pytest.mark.asyncio
async def test_acquire_familia_bundle_releases_on_load_failure(monkeypatch):
    """Si load_slot() tira tras tomar el semáforo, el permiso y el slot NO
    deben quedar colgados (sin leak de capacidad)."""
    import asyncio
    from worker import session_pool as sp

    store = _patch(monkeypatch, sp)
    pool = sp.SessionPool(_make_config(proxy_pool_size=1))
    await pool.initialize()

    store.load_slot = MagicMock(side_effect=RuntimeError("store corrupto"))
    sem_before = pool._sem._value

    with pytest.raises(RuntimeError):
        await pool.acquire_familia_bundle()

    assert pool._sem._value == sem_before  # semáforo no se filtró
    assert all(not s.busy for s in pool._slots)  # slot liberado
