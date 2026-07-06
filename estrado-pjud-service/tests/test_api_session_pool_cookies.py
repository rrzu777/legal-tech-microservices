import asyncio
from unittest.mock import MagicMock
from app.cookie_store import CookieBundle


def test_acquire_injects_cookies_from_store(monkeypatch):
    from app import session_pool as sp
    from app.session_pool import APISessionPool
    from app.config import Settings

    settings = Settings(API_KEY="t", _env_file=None)
    pool = APISessionPool(settings)

    pool._store = MagicMock()
    pool._store.load.return_value = CookieBundle(cookies={"TSPD_101": "z"}, user_agent="UAx", saved_at=9e9)

    captured = {}

    def fake_adapter(s, user_agent=None, cookies=None):
        captured["ua"] = user_agent
        captured["cookies"] = cookies
        return MagicMock()

    monkeypatch.setattr(sp, "OJVHttpAdapter", fake_adapter)

    class FakeSession:
        def __init__(self, adapter):
            pass
        async def initialize(self):
            pass
        async def close(self):
            pass

    monkeypatch.setattr(sp, "OJVSession", FakeSession)

    asyncio.run(pool.acquire())
    assert captured["cookies"] == {"TSPD_101": "z"}
    assert captured["ua"] == "UAx"
