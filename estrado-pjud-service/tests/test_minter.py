from app.cookie_scope import CookieRecord, playwright_cookie_records
from app.minter import MintResult


def test_cookies_to_dict_extracts_name_value():
    pw_cookies = [
        {"name": "TSPD_101", "value": "abc", "domain": "oficinajudicialvirtual.pjud.cl"},
        {"name": "PHPSESSID", "value": "xyz", "domain": "oficinajudicialvirtual.pjud.cl"},
    ]
    result = playwright_cookie_records(pw_cookies)
    assert result == (
        CookieRecord("TSPD_101", "abc", "oficinajudicialvirtual.pjud.cl", "/"),
        CookieRecord("PHPSESSID", "xyz", "oficinajudicialvirtual.pjud.cl", "/"),
    )


def test_mint_result_holds_cookies_and_ua():
    record = CookieRecord("TSPD_101", "abc", "pjud.cl", "/")
    r = MintResult(cookies=(record,), user_agent="UA/1.0")
    assert r.cookies == (record,)
    assert r.user_agent == "UA/1.0"
