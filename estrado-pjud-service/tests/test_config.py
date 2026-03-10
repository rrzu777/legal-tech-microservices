import os
import pytest


def test_config_loads_from_env(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key-123")
    monkeypatch.setenv("OJV_BASE_URL", "https://example.com")
    monkeypatch.setenv("RATE_LIMIT_MS", "3000")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    from app.config import Settings
    s = Settings()

    assert s.API_KEY == "test-key-123"
    assert s.OJV_BASE_URL == "https://example.com"
    assert s.RATE_LIMIT_MS == 3000
    assert s.LOG_LEVEL == "DEBUG"


def test_config_defaults(monkeypatch):
    monkeypatch.setenv("API_KEY", "key")

    from app.config import Settings
    s = Settings(_env_file=None)

    assert s.OJV_BASE_URL == "https://oficinajudicialvirtual.pjud.cl"
    assert s.RATE_LIMIT_MS == 2500
    assert s.LOG_LEVEL == "INFO"
    assert s.SESSION_POOL_SIZE == 2
    assert s.SESSION_MAX_AGE_S == 1200
