import asyncio
from unittest.mock import MagicMock
from app.cookie_store import CookieBundle


class _FakeSession:
    def __init__(self, adapter):
        self.adapter = adapter

    async def initialize(self):
        pass

    async def close(self):
        pass


def test_acquire_injects_cookies_from_store(monkeypatch):
    """Single-slot store (multi-bundle store with exactly one bundle) still
    injects that bundle's cookies/UA into the adapter."""
    from app import session_pool as sp
    from app.session_pool import APISessionPool
    from app.config import Settings

    settings = Settings(API_KEY="t", _env_file=None)
    pool = APISessionPool(settings)

    pool._store = MagicMock()
    pool._store.load_all.return_value = {
        "0": CookieBundle(cookies={"TSPD_101": "z"}, user_agent="UAx", saved_at=9e9)
    }

    captured = {}

    def fake_adapter(s, proxy=None, user_agent=None, cookies=None):
        captured["ua"] = user_agent
        captured["cookies"] = cookies
        captured["proxy"] = proxy
        return MagicMock()

    monkeypatch.setattr(sp, "OJVHttpAdapter", fake_adapter)
    monkeypatch.setattr(sp, "OJVSession", _FakeSession)

    asyncio.run(pool.acquire())
    assert captured["cookies"] == {"TSPD_101": "z"}
    assert captured["ua"] == "UAx"


def _bundle(proxy_url, tag):
    return CookieBundle(
        cookies={"TSPD_101": f"tok-{tag}"},
        user_agent=f"UA-{tag}",
        saved_at=9e9,
        proxy_url=proxy_url,
    )


def test_acquire_uses_proxy_url_from_multi_bundle_store(monkeypatch):
    """With bundles present in the multi-bundle store, acquire() must build
    the adapter with the `proxy` kwarg set to one of the bundles' proxy_url,
    and cookies/UA sourced from that same bundle."""
    from app import session_pool as sp
    from app.session_pool import APISessionPool
    from app.config import Settings

    settings = Settings(API_KEY="t", _env_file=None)
    pool = APISessionPool(settings)

    bundles = {
        "0": _bundle("http://u:p@proxy0:1", "0"),
        "1": _bundle("http://u:p@proxy1:1", "1"),
        "2": _bundle("http://u:p@proxy2:1", "2"),
    }
    pool._store = MagicMock()
    pool._store.load_all.return_value = bundles

    captured = []

    def fake_adapter(s, proxy=None, user_agent=None, cookies=None):
        captured.append({"proxy": proxy, "user_agent": user_agent, "cookies": cookies})
        return MagicMock()

    monkeypatch.setattr(sp, "OJVHttpAdapter", fake_adapter)
    monkeypatch.setattr(sp, "OJVSession", _FakeSession)

    asyncio.run(pool.acquire())

    assert len(captured) == 1
    call = captured[0]
    assert call["proxy"] in {b.proxy_url for b in bundles.values()}
    # cookies/UA must come from the SAME bundle as the chosen proxy
    matching = [b for b in bundles.values() if b.proxy_url == call["proxy"]]
    assert matching[0].cookies == call["cookies"]
    assert matching[0].user_agent == call["user_agent"]


def test_acquire_round_robins_across_slot_bundles(monkeypatch):
    """Creating fresh sessions repeatedly (bypassing the reuse deque via
    healthy=False release) must cycle through the distinct proxy_urls of the
    available slot bundles, one per slot, before repeating."""
    from app import session_pool as sp
    from app.session_pool import APISessionPool
    from app.config import Settings

    settings = Settings(API_KEY="t", _env_file=None)
    pool = APISessionPool(settings)

    bundles = {
        "0": _bundle("http://u:p@proxy0:1", "0"),
        "1": _bundle("http://u:p@proxy1:1", "1"),
        "2": _bundle("http://u:p@proxy2:1", "2"),
    }
    pool._store = MagicMock()
    pool._store.load_all.return_value = bundles

    captured_proxies = []

    def fake_adapter(s, proxy=None, user_agent=None, cookies=None):
        captured_proxies.append(proxy)
        return MagicMock()

    monkeypatch.setattr(sp, "OJVHttpAdapter", fake_adapter)
    monkeypatch.setattr(sp, "OJVSession", _FakeSession)

    async def _run():
        for _ in range(3):
            session = await pool.acquire()
            # force a brand-new session next time (bypass reuse deque)
            await pool.release(session, healthy=False)

    asyncio.run(_run())

    expected = sorted(b.proxy_url for b in bundles.values())
    assert sorted(captured_proxies) == expected
    assert len(set(captured_proxies)) == 3


def test_acquire_falls_back_to_no_proxy_when_store_empty(monkeypatch):
    """When load_all() returns {} (worker hasn't minted any slot yet), acquire()
    must still work, building the adapter with proxy=None (graceful
    degradation to today's no-proxy behavior)."""
    from app import session_pool as sp
    from app.session_pool import APISessionPool
    from app.config import Settings

    settings = Settings(API_KEY="t", _env_file=None)
    pool = APISessionPool(settings)

    pool._store = MagicMock()
    pool._store.load_all.return_value = {}

    captured = {}

    def fake_adapter(s, proxy=None, user_agent=None, cookies=None):
        captured["proxy"] = proxy
        captured["cookies"] = cookies
        captured["ua"] = user_agent
        return MagicMock()

    monkeypatch.setattr(sp, "OJVHttpAdapter", fake_adapter)
    monkeypatch.setattr(sp, "OJVSession", _FakeSession)

    session = asyncio.run(pool.acquire())

    assert captured["proxy"] is None
    assert captured["cookies"] is None
    assert captured["ua"] is None
    assert session is not None
