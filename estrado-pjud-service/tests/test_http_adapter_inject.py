from app.adapters.http_adapter import OJVHttpAdapter
from app.config import Settings


def _settings():
    return Settings(API_KEY="t", OJV_BASE_URL="https://x", RATE_LIMIT_MS=0, _env_file=None)


def test_adapter_uses_injected_user_agent():
    a = OJVHttpAdapter(_settings(), user_agent="Custom/9.9")
    assert a._client.headers["User-Agent"] == "Custom/9.9"


def test_adapter_seeds_cookies():
    a = OJVHttpAdapter(_settings(), cookies={"TSPD_101": "abc"})
    assert a._client.cookies.get("TSPD_101") == "abc"


def test_adapter_defaults_ua_when_none():
    a = OJVHttpAdapter(_settings())
    assert "Chrome" in a._client.headers["User-Agent"]
