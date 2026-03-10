"""Tests for API session pool health tracking."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from app.config import get_settings
    get_settings.cache_clear()


def _make_mock_session(age=0):
    session = MagicMock()
    session.age_seconds = age
    session.close = AsyncMock()
    return session


class TestAPISessionPool:
    @pytest.mark.asyncio
    async def test_release_healthy_returns_to_pool(self):
        from app.session_pool import APISessionPool
        from app.config import Settings
        settings = Settings(API_KEY="test", _env_file=None)
        pool = APISessionPool(settings)

        session = _make_mock_session(age=10)
        await pool.release(session, healthy=True)

        assert len(pool._pool) == 1
        session.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_release_unhealthy_closes_session(self):
        from app.session_pool import APISessionPool
        from app.config import Settings
        settings = Settings(API_KEY="test", _env_file=None)
        pool = APISessionPool(settings)

        session = _make_mock_session(age=10)
        await pool.release(session, healthy=False)

        assert len(pool._pool) == 0
        session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_release_default_is_healthy(self):
        from app.session_pool import APISessionPool
        from app.config import Settings
        settings = Settings(API_KEY="test", _env_file=None)
        pool = APISessionPool(settings)

        session = _make_mock_session(age=10)
        await pool.release(session)

        assert len(pool._pool) == 1
