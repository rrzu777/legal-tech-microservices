import os
import stat

from app.cookie_store import CookieStore

DUMMY_PROXY_0 = "http://user:pw_country-cl_session-tok0_lifetime-1h@geo.example.com:12321"
DUMMY_PROXY_1 = "http://user:pw_country-cl_session-tok1_lifetime-1h@geo.example.com:12321"


def test_save_slot_and_load_slot_roundtrip(tmp_path):
    store = CookieStore(path=str(tmp_path / "cookies.json"))
    store.save_slot("0", cookies={"TSPD_101": "abc"}, user_agent="UA/1.0", proxy_url=DUMMY_PROXY_0)
    bundle = store.load_slot("0")
    assert bundle.cookies == {"TSPD_101": "abc"}
    assert bundle.user_agent == "UA/1.0"
    assert bundle.proxy_url == DUMMY_PROXY_0


def test_multiple_slots_coexist_and_resave_does_not_wipe_others(tmp_path):
    store = CookieStore(path=str(tmp_path / "cookies.json"))
    store.save_slot("0", cookies={"a": "1"}, user_agent="UA/0", proxy_url=DUMMY_PROXY_0)
    store.save_slot("1", cookies={"b": "2"}, user_agent="UA/1", proxy_url=DUMMY_PROXY_1)

    all_bundles = store.load_all()
    assert set(all_bundles.keys()) == {"0", "1"}
    assert all_bundles["0"].cookies == {"a": "1"}
    assert all_bundles["0"].proxy_url == DUMMY_PROXY_0
    assert all_bundles["1"].cookies == {"b": "2"}
    assert all_bundles["1"].proxy_url == DUMMY_PROXY_1

    # Re-saving slot "0" must not wipe slot "1"
    store.save_slot("0", cookies={"a": "new"}, user_agent="UA/0-new", proxy_url=DUMMY_PROXY_0)
    all_bundles = store.load_all()
    assert set(all_bundles.keys()) == {"0", "1"}
    assert all_bundles["0"].cookies == {"a": "new"}
    assert all_bundles["1"].cookies == {"b": "2"}


def test_load_slot_absent_returns_none(tmp_path):
    store = CookieStore(path=str(tmp_path / "cookies.json"))
    store.save_slot("0", cookies={"a": "1"}, user_agent="UA/0", proxy_url=DUMMY_PROXY_0)
    assert store.load_slot("does-not-exist") is None


def test_load_all_on_missing_file_returns_empty(tmp_path):
    store = CookieStore(path=str(tmp_path / "nope.json"))
    assert store.load_all() == {}
    assert store.load_slot("0") is None


def test_corrupt_json_returns_empty_no_crash(tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text("{oops not json")
    store = CookieStore(path=str(p))
    assert store.load_all() == {}
    assert store.load_slot("0") is None


def test_old_single_bundle_format_treated_as_empty(tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text('{"cookies": {"TSPD_101": "abc"}, "user_agent": "UA/1.0", "saved_at": 123}')
    store = CookieStore(path=str(p))
    assert store.load_all() == {}
    assert store.load_slot("0") is None


def test_saved_file_is_group_world_readable(tmp_path):
    p = tmp_path / "cookies.json"
    CookieStore(path=str(p)).save_slot("0", cookies={"a": "1"}, user_agent="UA", proxy_url=DUMMY_PROXY_0)
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert oct(os.stat(p).st_mode)[-3:] == "644"
    assert mode == 0o644


def test_age_seconds_works_on_slot_bundle(tmp_path):
    store = CookieStore(path=str(tmp_path / "cookies.json"))
    store.save_slot("0", cookies={"a": "1"}, user_agent="UA", proxy_url=DUMMY_PROXY_0)
    bundle = store.load_slot("0")
    assert bundle.age_seconds >= 0
    assert bundle.age_seconds < 5


def test_proxy_url_defaults_to_none_when_saved_without_it(tmp_path):
    store = CookieStore(path=str(tmp_path / "cookies.json"))
    store.save_slot("0", cookies={"a": "1"}, user_agent="UA", proxy_url=None)
    bundle = store.load_slot("0")
    assert bundle.proxy_url is None


def test_slot_id_coerced_from_int(tmp_path):
    store = CookieStore(path=str(tmp_path / "cookies.json"))
    store.save_slot(0, cookies={"a": "1"}, user_agent="UA", proxy_url=DUMMY_PROXY_0)
    bundle = store.load_slot(0)
    assert bundle is not None
    assert bundle.cookies == {"a": "1"}
    all_bundles = store.load_all()
    assert "0" in all_bundles
