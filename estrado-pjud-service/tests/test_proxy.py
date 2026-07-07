"""Tests para app/proxy.py: builder de URL sticky + split para Playwright + token.

IMPORTANTE: solo URLs/credenciales DUMMY. Nunca credenciales reales de IPRoyal.
"""
import string

from app.proxy import (
    generate_session_token,
    build_sticky_proxy_url,
    split_proxy_for_playwright,
)

DUMMY_BASE_URL = "http://user123:pw_country-cl@geo.iproyal.com:12321"

ALPHANUMERIC = set(string.ascii_lowercase + string.digits)


class TestGenerateSessionToken:
    def test_default_length_is_8(self):
        token = generate_session_token()
        assert len(token) == 8

    def test_custom_length(self):
        token = generate_session_token(16)
        assert len(token) == 16

    def test_charset_is_alphanumeric(self):
        token = generate_session_token(32)
        assert set(token) <= ALPHANUMERIC

    def test_two_calls_differ(self):
        token_a = generate_session_token()
        token_b = generate_session_token()
        assert token_a != token_b


class TestBuildStickyProxyUrl:
    def test_appends_session_and_lifetime_suffix(self):
        result = build_sticky_proxy_url(DUMMY_BASE_URL, "abc12345")
        assert result.endswith("_session-abc12345_lifetime-1h@geo.iproyal.com:12321")

    def test_preserves_scheme_host_port_username(self):
        result = build_sticky_proxy_url(DUMMY_BASE_URL, "abc12345")
        assert result.startswith("http://user123:")
        assert "@geo.iproyal.com:12321" in result

    def test_preserves_country_segment_before_suffix(self):
        result = build_sticky_proxy_url(DUMMY_BASE_URL, "abc12345")
        # password original "pw_country-cl" debe seguir intacto antes del sufijo
        assert "pw_country-cl_session-abc12345_lifetime-1h" in result

    def test_custom_lifetime_is_respected(self):
        result = build_sticky_proxy_url(DUMMY_BASE_URL, "abc12345", lifetime="30m")
        assert result.endswith("_session-abc12345_lifetime-30m@geo.iproyal.com:12321")
        assert "lifetime-1h" not in result


class TestSplitProxyForPlaywright:
    def test_splits_server_username_password(self):
        proxy_url = (
            "http://user123:pw_country-cl_session-abc12345_lifetime-1h"
            "@geo.iproyal.com:12321"
        )
        result = split_proxy_for_playwright(proxy_url)
        assert result == {
            "server": "http://geo.iproyal.com:12321",
            "username": "user123",
            "password": "pw_country-cl_session-abc12345_lifetime-1h",
        }


class TestRoundTrip:
    def test_build_then_split_yields_correct_parts(self):
        sticky_url = build_sticky_proxy_url(DUMMY_BASE_URL, "tok12345")
        result = split_proxy_for_playwright(sticky_url)
        assert result["server"] == "http://geo.iproyal.com:12321"
        assert result["username"] == "user123"
        assert result["password"].endswith("_session-tok12345_lifetime-1h")
        assert result["password"].startswith("pw_country-cl")
