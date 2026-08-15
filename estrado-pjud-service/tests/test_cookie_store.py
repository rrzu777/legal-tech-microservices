from app.cookie_scope import CookieRecord
from app.cookie_store import CookieStore


def _legacy_record(name: str, value: str) -> CookieRecord:
    return CookieRecord(
        name=name,
        value=value,
        domain="oficinajudicialvirtual.pjud.cl",
        path="/",
        secure=True,
    )


def test_save_and_load_roundtrip(tmp_path):
    store = CookieStore(path=str(tmp_path / "cookies.json"))
    store.save(cookies={"TSPD_101": "abc"}, user_agent="UA/1.0")
    bundle = store.load()
    assert bundle.cookies == (_legacy_record("TSPD_101", "abc"),)
    assert bundle.user_agent == "UA/1.0"


def test_load_missing_returns_none(tmp_path):
    store = CookieStore(path=str(tmp_path / "nope.json"))
    assert store.load() is None


def test_saved_file_is_group_readable_but_not_world_readable(tmp_path):
    import os
    import stat
    p = tmp_path / "cookies.json"
    CookieStore(path=str(p)).save(cookies={"TSPD_101": "x"}, user_agent="UA")
    mode = stat.S_IMODE(os.stat(p).st_mode)
    # Worker (estrado) escribe; API lee vía SupplementaryGroups=estrado.
    assert mode == 0o640


def test_load_malformed_json_returns_none(tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text("{oops not json")
    assert CookieStore(path=str(p)).load() is None


def test_load_wrong_shape_returns_none(tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text('{"unexpected": "shape"}')  # valid JSON, missing keys
    assert CookieStore(path=str(p)).load() is None


def test_load_rejects_bundle_saved_meaningfully_in_the_future(tmp_path, monkeypatch):
    from app import cookie_store as cookie_store_module

    now = 10_000.0
    monkeypatch.setattr(cookie_store_module.time, "time", lambda: now)
    path = tmp_path / "cookies.json"
    store = CookieStore(path=str(path))
    store.save_slot(
        0,
        cookies={"TSPD_101": "abc"},
        user_agent="UA/1.0",
        proxy_token="sticky",
    )
    payload = path.read_text().replace(
        f'"saved_at": {now}',
        f'"saved_at": {now + 61}',
    )
    path.write_text(payload)

    assert store.load_slot(0) is None


def test_age_seconds_reflects_save_time(tmp_path):
    store = CookieStore(path=str(tmp_path / "cookies.json"))
    store.save(cookies={"TSPD_101": "abc"}, user_agent="UA/1.0")
    bundle = store.load()
    assert bundle.age_seconds >= 0
    assert bundle.age_seconds < 5


def test_replace_slot_cookies_if_current_preserves_bundle_identity(tmp_path):
    path = tmp_path / "cookies.json"
    store = CookieStore(path=str(path))
    store.save_slot(
        0,
        cookies={"PHPSESSID": "old"},
        user_agent="UA/1.0",
        proxy_token="sticky",
    )
    before = store.load_slot(0)

    replaced = store.replace_slot_cookies_if_current(
        0,
        expected_saved_at=before.saved_at,
        expected_proxy_token="sticky",
        cookies={"PHPSESSID": "renewed"},
    )

    after = store.load_slot(0)
    assert replaced is True
    assert after.saved_at == before.saved_at
    assert after.proxy_token == "sticky"
    assert after.user_agent == "UA/1.0"
    assert [(cookie.name, cookie.value) for cookie in after.cookies] == [
        ("PHPSESSID", "renewed"),
    ]


def test_replace_slot_cookies_if_current_mismatch_is_byte_preserving(tmp_path):
    path = tmp_path / "cookies.json"
    store = CookieStore(path=str(path))
    store.save_slot(
        0,
        cookies={"PHPSESSID": "old"},
        user_agent="UA/1.0",
        proxy_token="sticky",
    )
    before = store.load_slot(0)
    original = path.read_bytes()

    assert store.replace_slot_cookies_if_current(
        0,
        expected_saved_at=before.saved_at + 1,
        expected_proxy_token="sticky",
        cookies={"PHPSESSID": "wrong-saved-at"},
    ) is False
    assert path.read_bytes() == original

    assert store.replace_slot_cookies_if_current(
        0,
        expected_saved_at=before.saved_at,
        expected_proxy_token="other-sticky",
        cookies={"PHPSESSID": "wrong-token"},
    ) is False
    assert path.read_bytes() == original


def test_replace_slot_cookies_if_current_missing_or_invalid_is_non_mutating(tmp_path):
    path = tmp_path / "cookies.json"
    path.write_text('{"version": 2, "slots": {"0": {"saved_at": "bad"}}}')
    store = CookieStore(path=str(path))
    original = path.read_bytes()

    assert store.replace_slot_cookies_if_current(
        0,
        expected_saved_at=1.0,
        expected_proxy_token="sticky",
        cookies={"PHPSESSID": "new"},
    ) is False
    assert path.read_bytes() == original

    assert store.replace_slot_cookies_if_current(
        1,
        expected_saved_at=1.0,
        expected_proxy_token="sticky",
        cookies={"PHPSESSID": "new"},
    ) is False
    assert path.read_bytes() == original
