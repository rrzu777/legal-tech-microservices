from app.minter import cookies_to_dict, MintResult


def test_cookies_to_dict_extracts_name_value():
    pw_cookies = [
        {"name": "TSPD_101", "value": "abc", "domain": "oficinajudicialvirtual.pjud.cl"},
        {"name": "PHPSESSID", "value": "xyz", "domain": "oficinajudicialvirtual.pjud.cl"},
    ]
    result = cookies_to_dict(pw_cookies)
    assert result == {"TSPD_101": "abc", "PHPSESSID": "xyz"}


def test_mint_result_holds_cookies_and_ua():
    r = MintResult(cookies={"TSPD_101": "abc"}, user_agent="UA/1.0")
    assert r.cookies["TSPD_101"] == "abc"
    assert r.user_agent == "UA/1.0"
