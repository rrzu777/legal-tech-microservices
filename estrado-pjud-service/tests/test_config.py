import os
import pytest


@pytest.mark.parametrize("value", [
    "bad",
    "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
    " aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "aaaaaaaa-aaaa-1aaa-8aaa-aaaaaaaaaaaa",
    "aaaaaaaa-aaaa-4aaa-7aaa-aaaaaaaaaaaa",
])
def test_runtime_generation_rejects_noncanonical_identity(value):
    from app.config import Settings
    with pytest.raises(ValueError, match="pjud_runtime_invalid_generation"):
        Settings(API_KEY="synthetic", PJUD_RUNTIME_GENERATION=value, _env_file=None)


def test_runtime_generation_legacy_blank_and_explicit_identity():
    from app.config import Settings
    assert Settings(API_KEY="synthetic", PJUD_RUNTIME_GENERATION=" ", _env_file=None).PJUD_RUNTIME_GENERATION is None
    assert Settings(API_KEY="synthetic", PJUD_RUNTIME_GENERATION="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", _env_file=None).PJUD_RUNTIME_GENERATION == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


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


@pytest.mark.parametrize("obsolete_env", [
    "PJUD_CATALOG_OPPORTUNISTIC_ENABLED",
    "PJUD_CATALOG_QUEUE_SIZE",
    "PJUD_CATALOG_LEASE_SECONDS",
    "PJUD_CATALOG_COOLDOWN_SECONDS",
])
def test_obsolete_catalog_refresh_env_cannot_configure_runtime(
    monkeypatch, obsolete_env,
):
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setenv(obsolete_env, "1")

    from app.config import Settings

    settings = Settings(_env_file=None)

    assert not hasattr(settings, obsolete_env)


def test_config_rejects_cookie_store_inside_production_checkout(monkeypatch):
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setenv(
        "COOKIE_STORE_PATH",
        "/opt/legal-tech-microservices/estrado-pjud-service/.cookies.json",
    )

    from app.config import Settings

    with pytest.raises(ValueError, match="outside the git checkout"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        ".cookies.json",
        "cookies.json",
        "/var/lib/../../opt/legal-tech-microservices/estrado-pjud-service/.cookies.json",
    ],
)
def test_config_rejects_relative_or_normalized_checkout_path(monkeypatch, unsafe_path):
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setenv("COOKIE_STORE_PATH", unsafe_path)

    from app.config import Settings

    with pytest.raises(ValueError, match="absolute and outside the git checkout"):
        Settings(_env_file=None)


def test_telegram_config_defaults(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from app.config import get_settings, Settings
    get_settings.cache_clear()
    settings = Settings(API_KEY="test", _env_file=None)
    assert settings.TELEGRAM_BOT_TOKEN == ""
    assert settings.TELEGRAM_CHAT_ID == ""
    assert settings.TELEGRAM_BLOCKED_RATE_THRESHOLD == 0.3
    assert settings.TELEGRAM_COOLDOWN_S == 300
