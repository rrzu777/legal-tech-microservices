from unittest.mock import AsyncMock, MagicMock
import pytest

from app.minter import MintResult
from app.cookie_store import CookieBundle


@pytest.mark.asyncio
async def test_reuses_fresh_store_without_minting():
    from worker.session_pool import get_or_mint_cookies
    store = MagicMock()
    store.load.return_value = CookieBundle(cookies={"TSPD_101": "a"}, user_agent="UA", saved_at=9e9)
    minter = MagicMock()
    minter.mint = AsyncMock()
    result = await get_or_mint_cookies(store, minter, max_age_s=1500)
    minter.mint.assert_not_awaited()
    assert result.cookies == {"TSPD_101": "a"}
    assert result.user_agent == "UA"


@pytest.mark.asyncio
async def test_mints_and_saves_when_store_empty():
    from worker.session_pool import get_or_mint_cookies
    store = MagicMock()
    store.load.return_value = None
    minter = MagicMock()
    minter.mint = AsyncMock(return_value=MintResult(cookies={"TSPD_101": "b"}, user_agent="UA2"))
    result = await get_or_mint_cookies(store, minter, max_age_s=1500)
    minter.mint.assert_awaited_once()
    store.save.assert_called_once_with(cookies={"TSPD_101": "b"}, user_agent="UA2")
    assert result.cookies == {"TSPD_101": "b"}
