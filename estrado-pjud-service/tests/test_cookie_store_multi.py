import os
import stat
import json

import pytest

import app.cookie_store as cookie_store_module
from app.cookie_store import CookieStore
from app.cookie_scope import CookieRecord

DUMMY_PROXY_0 = "http://user:pw_country-cl_session-tok0_lifetime-1h@geo.example.com:12321"
DUMMY_PROXY_1 = "http://user:pw_country-cl_session-tok1_lifetime-1h@geo.example.com:12321"
DUMMY_TOKEN_0 = "tok0"
DUMMY_TOKEN_1 = "tok1"


def _legacy_record(name: str, value: str) -> CookieRecord:
    return CookieRecord(
        name=name,
        value=value,
        domain="oficinajudicialvirtual.pjud.cl",
        path="/",
        secure=True,
    )


def test_v2_roundtrip_preserves_same_name_distinct_scopes(tmp_path):
    path = tmp_path / "cookies.json"
    store = CookieStore(path=str(path), legacy_cookie_domain="oficinajudicialvirtual.pjud.cl")
    records = (
        CookieRecord("PHPSESSID", "root", "oficinajudicialvirtual.pjud.cl", "/", True),
        CookieRecord(
            "PHPSESSID", "form", "oficinajudicialvirtual.pjud.cl",
            "/consultaUnificada.php", True, 1_800_000_000, True, "Lax",
        ),
    )

    store.save_slot("0", records, "UA/1.0", DUMMY_TOKEN_0)

    assert store.load_slot("0").cookies == records
    raw = json.loads(path.read_text())
    assert raw["version"] == 2
    assert raw["slots"]["0"]["cookies"][1]["path"] == "/consultaUnificada.php"


def test_first_write_migrates_all_valid_legacy_slots_to_v2(tmp_path):
    path = tmp_path / "cookies.json"
    path.write_text(
        '{"slots":{"0":{"cookies":{"legacy":"one"},"user_agent":"UA/0",'
        '"proxy_token":"tok0","saved_at":123},"1":{"cookies":{"legacy":"two"},'
        '"user_agent":"UA/1","proxy_token":"tok1","saved_at":456}}}'
    )
    store = CookieStore(path=str(path), legacy_cookie_domain="oficinajudicialvirtual.pjud.cl")

    store.save_slot("2", (
        CookieRecord("fresh", "three", "oficinajudicialvirtual.pjud.cl", "/", True),
    ), "UA/2", "tok2")

    raw = json.loads(path.read_text())
    assert raw["version"] == 2
    assert set(raw["slots"]) == {"0", "1", "2"}
    assert raw["slots"]["0"]["cookies"] == [{
        "name": "legacy", "value": "one",
        "domain": "oficinajudicialvirtual.pjud.cl", "path": "/",
        "secure": True, "expires": None, "http_only": False, "same_site": None,
    }]


def test_save_slot_and_load_slot_roundtrip(tmp_path):
    store = CookieStore(path=str(tmp_path / "cookies.json"))
    store.save_slot("0", cookies={"TSPD_101": "abc"}, user_agent="UA/1.0", proxy_token=DUMMY_TOKEN_0)
    bundle = store.load_slot("0")
    assert bundle.cookies == (_legacy_record("TSPD_101", "abc"),)
    assert bundle.user_agent == "UA/1.0"
    assert bundle.proxy_url is None
    assert bundle.proxy_token == DUMMY_TOKEN_0


def test_multiple_slots_coexist_and_resave_does_not_wipe_others(tmp_path):
    store = CookieStore(path=str(tmp_path / "cookies.json"))
    store.save_slot("0", cookies={"a": "1"}, user_agent="UA/0", proxy_token=DUMMY_TOKEN_0)
    store.save_slot("1", cookies={"b": "2"}, user_agent="UA/1", proxy_token=DUMMY_TOKEN_1)

    all_bundles = store.load_all()
    assert set(all_bundles.keys()) == {"0", "1"}
    assert all_bundles["0"].cookies == (_legacy_record("a", "1"),)
    assert all_bundles["0"].proxy_token == DUMMY_TOKEN_0
    assert all_bundles["1"].cookies == (_legacy_record("b", "2"),)
    assert all_bundles["1"].proxy_token == DUMMY_TOKEN_1

    # Re-saving slot "0" must not wipe slot "1"
    store.save_slot("0", cookies={"a": "new"}, user_agent="UA/0-new", proxy_token=DUMMY_TOKEN_0)
    all_bundles = store.load_all()
    assert set(all_bundles.keys()) == {"0", "1"}
    assert all_bundles["0"].cookies == (_legacy_record("a", "new"),)
    assert all_bundles["1"].cookies == (_legacy_record("b", "2"),)


def test_load_slot_absent_returns_none(tmp_path):
    store = CookieStore(path=str(tmp_path / "cookies.json"))
    store.save_slot("0", cookies={"a": "1"}, user_agent="UA/0", proxy_token=DUMMY_TOKEN_0)
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


def test_old_single_bundle_format_loads_as_slot_zero(tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text('{"cookies": {"TSPD_101": "abc"}, "user_agent": "UA/1.0", "saved_at": 123}')
    store = CookieStore(path=str(p))
    assert store.load_slot("0").cookies == (_legacy_record("TSPD_101", "abc"),)


def test_saved_file_is_group_readable_but_not_world_readable(tmp_path):
    p = tmp_path / "cookies.json"
    CookieStore(path=str(p)).save_slot("0", cookies={"a": "1"}, user_agent="UA", proxy_token=DUMMY_TOKEN_0)
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert oct(os.stat(p).st_mode)[-3:] == "640"
    assert mode == 0o640


def test_new_lock_file_is_group_readable_but_not_world_readable(tmp_path):
    p = tmp_path / "cookies.json"
    CookieStore(path=str(p)).save_slot(
        "0", cookies={"a": "1"}, user_agent="UA", proxy_token=DUMMY_TOKEN_0
    )

    mode = stat.S_IMODE(os.stat(f"{p}.lock").st_mode)

    assert mode == 0o640


def test_existing_lock_inode_is_not_repermissioned(tmp_path, monkeypatch):
    p = tmp_path / "cookies.json"
    lock_path = tmp_path / "cookies.json.lock"
    lock_path.touch(mode=0o600)
    os.chmod(lock_path, 0o600)
    calls = []

    def fail_if_repermissioned(*args):
        calls.append(args)
        raise AssertionError("existing_lock_inode_was_repermissioned")

    monkeypatch.setattr(cookie_store_module.os, "fchmod", fail_if_repermissioned)

    with CookieStore(path=str(p))._exclusive_write_lock():
        pass

    assert calls == []


def test_existing_group_readable_lock_is_acquired_without_write_access(tmp_path, monkeypatch):
    """The API's estrado group can lock the worker-owned 0640 inode read-only."""
    p = tmp_path / "cookies.json"
    lock_path = tmp_path / "cookies.json.lock"
    lock_path.touch(mode=0o640)
    os.chmod(lock_path, 0o640)
    real_open = cookie_store_module.os.open

    def deny_existing_lock_write(path, flags, *args):
        if path == str(lock_path) and flags == os.O_RDWR:
            raise PermissionError("group_has_no_write_bit")
        return real_open(path, flags, *args)

    monkeypatch.setattr(cookie_store_module.os, "open", deny_existing_lock_write)

    with CookieStore(path=str(p))._exclusive_write_lock():
        pass


def test_store_never_persists_proxy_credentials(tmp_path):
    p = tmp_path / "cookies.json"
    CookieStore(path=str(p)).save_slot(
        "0", cookies={"a": "1"}, user_agent="UA", proxy_token=DUMMY_TOKEN_0
    )
    raw = p.read_text()
    assert DUMMY_TOKEN_0 in raw
    assert "http://user:" not in raw
    assert "pw_country" not in raw
    assert "proxy_url" not in raw


def test_legacy_proxy_url_is_never_loaded(tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text(
        '{"slots":{"0":{"cookies":{"a":"1"},"user_agent":"UA",'
        '"proxy_url":"http://user:secret@example.com:1","saved_at":123}}}'
    )
    bundle = CookieStore(path=str(p)).load_slot("0")
    assert bundle is not None
    assert bundle.proxy_url is None
    assert bundle.proxy_token is None


def test_age_seconds_works_on_slot_bundle(tmp_path):
    store = CookieStore(path=str(tmp_path / "cookies.json"))
    store.save_slot("0", cookies={"a": "1"}, user_agent="UA", proxy_token=DUMMY_TOKEN_0)
    bundle = store.load_slot("0")
    assert bundle.age_seconds >= 0
    assert bundle.age_seconds < 5


def test_proxy_token_defaults_to_none_when_saved_without_it(tmp_path):
    store = CookieStore(path=str(tmp_path / "cookies.json"))
    store.save_slot("0", cookies={"a": "1"}, user_agent="UA", proxy_token=None)
    bundle = store.load_slot("0")
    assert bundle.proxy_token is None


def test_slot_id_coerced_from_int(tmp_path):
    store = CookieStore(path=str(tmp_path / "cookies.json"))
    store.save_slot(0, cookies={"a": "1"}, user_agent="UA", proxy_token=DUMMY_TOKEN_0)
    bundle = store.load_slot(0)
    assert bundle is not None
    assert bundle.cookies == (_legacy_record("a", "1"),)
    all_bundles = store.load_all()
    assert "0" in all_bundles


def test_malformed_slot_entry_skipped_good_slots_survive(tmp_path):
    """Un slot malformado (no-dict / faltan keys) dentro de un {"slots"} válido
    NO debe crashear ni tumbar los slots sanos: se saltea el malo y se conservan
    los buenos. Es la ruta de robustez más propensa a regresión silenciosa."""
    p = tmp_path / "cookies.json"
    # slot "0" sano, "1" no-dict, "2" dict sin las keys esperadas
    p.write_text(
        '{"slots": {'
        '"0": {"cookies": {"a": "1"}, "user_agent": "UA/0", "proxy_token": null, "saved_at": 123},'
        '"1": "not-a-dict",'
        '"2": {"user_agent": "UA/2"}'
        '}}'
    )
    store = CookieStore(path=str(p))
    all_bundles = store.load_all()  # no debe lanzar
    assert set(all_bundles.keys()) == {"0"}
    assert all_bundles["0"].cookies == (_legacy_record("a", "1"),)
    assert store.load_slot("1") is None
    assert store.load_slot("2") is None


@pytest.mark.parametrize("field,value", [
    ("user_agent", None),
    ("user_agent", ""),
    ("saved_at", "123"),
    ("saved_at", float("nan")),
    ("saved_at", 10**1000),
    ("proxy_token", 123),
])
def test_invalid_bundle_metadata_is_rejected(tmp_path, field, value):
    p = tmp_path / "cookies.json"
    payload = {
        "version": 2,
        "slots": {"0": {
            "cookies": [{
                "name": "a", "value": "1", "domain": "ojv.test", "path": "/",
                "secure": True, "expires": None, "http_only": False, "same_site": None,
            }],
            "user_agent": "UA",
            "proxy_token": None,
            "saved_at": 123.0,
        }},
    }
    payload["slots"]["0"][field] = value
    p.write_text(json.dumps(payload))

    assert CookieStore(path=str(p)).load_all() == {}


def test_configured_legacy_scope_is_used_for_dict_bundle(tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text('{"slots":{"0":{"cookies":{"a":"1"},"user_agent":"UA",'
                 '"proxy_token":null,"saved_at":123}}}')
    store = CookieStore(path=str(p))

    store.configure_legacy_scope("http://ojv.local.test:8080")

    assert store.load_slot("0").cookies == (
        CookieRecord("a", "1", "ojv.local.test", "/", False),
    )


def test_oversized_timestamp_does_not_poison_healthy_slot(tmp_path):
    p = tmp_path / "cookies.json"
    cookie = [{
        "name": "a", "value": "1", "domain": "ojv.test", "path": "/",
        "secure": True, "expires": None, "http_only": False, "same_site": None,
    }]
    p.write_text(json.dumps({"version": 2, "slots": {
        "bad": {"cookies": cookie, "user_agent": "UA", "saved_at": 10**1000},
        "good": {"cookies": cookie, "user_agent": "UA", "saved_at": 123.0},
    }}))

    bundles = CookieStore(path=str(p)).load_all()

    assert set(bundles) == {"good"}
