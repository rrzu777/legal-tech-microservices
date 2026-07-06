from app.parsers.search_parser import detect_blocked


def test_bobcmn_challenge_is_blocked():
    html = '<html><head><script>window["bobcmn"] = "1011...";</script></head><body></body></html>'
    assert detect_blocked(html) is True


def test_tspd_path_challenge_is_blocked():
    html = '<html><body>reference /TSPD/ 08a336</body></html>'
    assert detect_blocked(html) is True


def test_real_form_is_not_blocked():
    html = '<html><body><select id="competencia"><option>Civil</option></select></body></html>'
    assert detect_blocked(html) is False
