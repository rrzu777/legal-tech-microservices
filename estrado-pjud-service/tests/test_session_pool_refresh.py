from unittest.mock import AsyncMock, MagicMock
import pytest


def _make_pool():
    from worker.session_pool import SessionPool
    config = MagicMock()
    config.COOKIE_STORE_PATH = "/tmp/does-not-matter.json"
    config.PJUD_BASE_URL = "https://x"
    config.RATE_LIMIT_MS = 0
    config.SESSION_MAX_AGE_S = 1500
    config.POOL_SIZE = 1
    config.OJV_PROXY_URL = None
    config.OJV_PROXY_STICKY_LIFETIME = "1h"
    config.OJV_PROXY_POOL_SIZE = 3
    config.BLOCK_PAUSE_S = 30
    return SessionPool(config)


@pytest.mark.asyncio
async def test_refresh_keeps_old_session_when_new_init_fails(monkeypatch):
    from worker import session_pool as sp
    from app.minter import MintResult

    pool = _make_pool()
    old = MagicMock()
    old.close = AsyncMock()
    slot = sp._Slot(index=0, token=None, proxy_url=None, session=old)
    pool._slots = [slot]

    class FakeMinter:
        def __init__(self, base_url, proxy=None):
            pass

        async def mint(self):
            return MintResult(cookies={"TSPD_101": "a"}, user_agent="UA")

    monkeypatch.setattr(sp, "CookieMinter", FakeMinter)
    monkeypatch.setattr(sp, "Settings", lambda **k: MagicMock())
    monkeypatch.setattr(sp, "OJVHttpAdapter", lambda *a, **k: MagicMock())

    class FailingSession:
        def __init__(self, adapter): pass
        async def initialize(self):
            raise RuntimeError("init failed")
        async def close(self):
            pass
    monkeypatch.setattr(sp, "OJVSession", FailingSession)

    with pytest.raises(RuntimeError):
        await pool._refresh_slot(slot)

    # The old session must NOT have been closed, and must still be on the slot.
    old.close.assert_not_awaited()
    assert slot.session is old
