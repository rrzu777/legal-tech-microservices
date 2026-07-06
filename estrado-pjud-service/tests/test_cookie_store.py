from app.cookie_store import CookieStore


def test_save_and_load_roundtrip(tmp_path):
    store = CookieStore(path=str(tmp_path / "cookies.json"))
    store.save(cookies={"TSPD_101": "abc"}, user_agent="UA/1.0")
    bundle = store.load()
    assert bundle.cookies == {"TSPD_101": "abc"}
    assert bundle.user_agent == "UA/1.0"


def test_load_missing_returns_none(tmp_path):
    store = CookieStore(path=str(tmp_path / "nope.json"))
    assert store.load() is None


def test_saved_file_is_group_world_readable(tmp_path):
    import os
    import stat
    p = tmp_path / "cookies.json"
    CookieStore(path=str(p)).save(cookies={"TSPD_101": "x"}, user_agent="UA")
    mode = stat.S_IMODE(os.stat(p).st_mode)
    # Worker (estrado) escribe, API (www-data) lee → debe ser legible por otros.
    assert mode == 0o644


def test_load_malformed_json_returns_none(tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text("{oops not json")
    assert CookieStore(path=str(p)).load() is None


def test_load_wrong_shape_returns_none(tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text('{"unexpected": "shape"}')  # valid JSON, missing keys
    assert CookieStore(path=str(p)).load() is None


def test_age_seconds_reflects_save_time(tmp_path):
    store = CookieStore(path=str(tmp_path / "cookies.json"))
    store.save(cookies={"TSPD_101": "abc"}, user_agent="UA/1.0")
    bundle = store.load()
    assert bundle.age_seconds >= 0
    assert bundle.age_seconds < 5
