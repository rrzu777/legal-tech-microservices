import os
import pytest


def test_app_settings_proxy_defaults(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")

    from app.config import Settings
    s = Settings(_env_file=None)

    assert s.OJV_PROXY_URL is None
    assert s.OJV_PROXY_STICKY_LIFETIME == "1h"


def test_app_settings_proxy_env_override(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("OJV_PROXY_URL", "http://dummy:dummy_country-cl@geo.example.com:12321")
    monkeypatch.setenv("OJV_PROXY_STICKY_LIFETIME", "30m")

    from app.config import Settings
    s = Settings(_env_file=None)

    assert s.OJV_PROXY_URL == "http://dummy:dummy_country-cl@geo.example.com:12321"
    assert s.OJV_PROXY_STICKY_LIFETIME == "30m"


def test_worker_config_proxy_defaults(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "eyJtest")

    from worker.config import WorkerConfig
    config = WorkerConfig(_env_file=None)

    assert config.OJV_PROXY_URL is None
    assert config.OJV_PROXY_STICKY_LIFETIME == "1h"
    assert config.OJV_PROXY_POOL_SIZE == 3
    assert config.OJV_PROXY_GB_BUDGET == 2.0
    assert config.OJV_PROXY_GB_ALERT_PCT == 80


def test_worker_config_proxy_env_override(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "eyJtest")
    monkeypatch.setenv("OJV_PROXY_URL", "http://dummy:dummy_country-cl@geo.example.com:12321")
    monkeypatch.setenv("OJV_PROXY_STICKY_LIFETIME", "30m")
    monkeypatch.setenv("OJV_PROXY_POOL_SIZE", "5")
    monkeypatch.setenv("OJV_PROXY_GB_BUDGET", "10.5")
    monkeypatch.setenv("OJV_PROXY_GB_ALERT_PCT", "90")

    from worker.config import WorkerConfig
    config = WorkerConfig(_env_file=None)

    assert config.OJV_PROXY_URL == "http://dummy:dummy_country-cl@geo.example.com:12321"
    assert config.OJV_PROXY_STICKY_LIFETIME == "30m"
    assert config.OJV_PROXY_POOL_SIZE == 5
    assert config.OJV_PROXY_GB_BUDGET == 10.5
    assert config.OJV_PROXY_GB_ALERT_PCT == 90
