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
    return SessionPool(config)


@pytest.mark.asyncio
async def test_refresh_keeps_old_session_when_new_init_fails(monkeypatch):
    from worker import session_pool as sp
    from app.minter import MintResult

    pool = _make_pool()
    old = MagicMock()
    old.close = AsyncMock()
    pool._pool = [old]

    async def fake_creds(*a, **k):
        return MintResult(cookies={"TSPD_101": "a"}, user_agent="UA")
    monkeypatch.setattr(sp, "get_or_mint_cookies", fake_creds)
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
        await pool._refresh_session(old)

    # The old session must NOT have been closed, and must still be in the pool.
    old.close.assert_not_awaited()
    assert pool._pool == [old]
