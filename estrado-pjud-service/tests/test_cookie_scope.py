from http.cookiejar import CookieJar

import pytest

from app.cookie_scope import (
    CookieRecord,
    cookie_jar_from_records,
    cookie_records_from_jar,
    normalize_cookie_records,
    playwright_cookie_records,
)


def test_playwright_records_preserve_same_name_with_distinct_scopes_and_values():
    records = playwright_cookie_records([
        {
            "name": "PHPSESSID", "value": "root", "domain": "pjud.cl", "path": "/",
            "secure": True, "expires": -1, "httpOnly": True, "sameSite": "Lax",
        },
        {
            "name": "PHPSESSID", "value": "form", "domain": "pjud.cl",
            "path": "/consultaUnificada.php", "secure": True, "expires": 1_800_000_000,
            "httpOnly": False, "sameSite": "Strict",
        },
    ])

    assert records == (
        CookieRecord("PHPSESSID", "root", "pjud.cl", "/", True, None, True, "Lax"),
        CookieRecord(
            "PHPSESSID", "form", "pjud.cl", "/consultaUnificada.php",
            True, 1_800_000_000, False, "Strict",
        ),
    )


def test_cookie_jar_round_trip_preserves_scopes_and_flags():
    records = (
        CookieRecord("session", "root", ".pjud.cl", "/", True, None, True, "Lax"),
        CookieRecord("session", "detail", ".pjud.cl", "/detail", False, 1_800_000_000),
    )

    jar = cookie_jar_from_records(records)

    assert isinstance(jar, CookieJar)
    assert cookie_records_from_jar(jar) == records


def test_same_scope_conflicting_value_fails_closed_without_disclosure():
    sentinels = ("name-sentinel", "first-sentinel", "second-sentinel", "domain-sentinel")
    with pytest.raises(ValueError, match="^ambiguous_cookie_scope$") as exc_info:
        normalize_cookie_records([
            CookieRecord(sentinels[0], sentinels[1], sentinels[3], "/"),
            CookieRecord(sentinels[0], sentinels[2], sentinels[3], "/"),
        ])
    assert all(value not in str(exc_info.value) for value in sentinels)


def test_exact_duplicate_is_deduplicated_in_first_seen_order():
    record = CookieRecord("a", "1", "pjud.cl", "/")
    assert normalize_cookie_records([record, record]) == (record,)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "", "value": "1", "domain": "pjud.cl", "path": "/"},
        {"name": "a", "value": None, "domain": "pjud.cl", "path": "/"},
        {"name": "a", "value": "1", "domain": "", "path": "/"},
        {"name": "a", "value": "1", "domain": "pjud.cl", "path": ""},
    ],
)
def test_invalid_record_fails_closed(kwargs):
    with pytest.raises(ValueError, match="^invalid_cookie_record$"):
        CookieRecord(**kwargs)
