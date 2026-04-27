from pathlib import Path
import pytest

from app.parsers.search_parser import parse_search_results, detect_blocked

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_bytes().decode("latin-1")


class TestParseSearchCivil:
    @pytest.fixture
    def html(self):
        return _load("search_Civil_C_1234_2024.html")

    def test_returns_list(self, html):
        results = parse_search_results(html, "civil")
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_each_match_has_required_fields(self, html):
        results = parse_search_results(html, "civil")
        for m in results:
            assert "key" in m and m["key"].startswith("eyJ")
            assert "rol" in m and m["rol"]
            assert "tribunal" in m and m["tribunal"]
            assert "caratulado" in m and m["caratulado"]
            assert "fecha_ingreso" in m

    def test_date_is_iso(self, html):
        results = parse_search_results(html, "civil")
        for m in results:
            if m["fecha_ingreso"]:
                assert len(m["fecha_ingreso"]) == 10
                assert m["fecha_ingreso"][4] == "-"


class TestParseSearchLaboral:
    @pytest.fixture
    def html(self):
        return _load("search_Laboral_T_500_2024.html")

    def test_returns_results(self, html):
        results = parse_search_results(html, "laboral")
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_jwt_keys_present(self, html):
        results = parse_search_results(html, "laboral")
        for m in results:
            assert m["key"].startswith("eyJ")


class TestParseSearchCobranza:
    @pytest.fixture
    def html(self):
        return _load("search_Cobranza_C_1000_2024.html")

    def test_returns_results(self, html):
        results = parse_search_results(html, "cobranza")
        assert isinstance(results, list)
        assert len(results) >= 1


class TestParseSearchSuprema:
    @pytest.fixture
    def html(self):
        return _load("search_Suprema_100_2025.html")

    def test_returns_results(self, html):
        results = parse_search_results(html, "suprema")
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_jwt_keys_present(self, html):
        results = parse_search_results(html, "suprema")
        for m in results:
            assert m["key"].startswith("eyJ")

    def test_fields_present(self, html):
        results = parse_search_results(html, "suprema")
        for m in results:
            assert m["rol"]
            assert m["tribunal"]
            assert m["caratulado"]


class TestParseSearchApelaciones:
    @pytest.fixture
    def html(self):
        return _load("search_Apelaciones_Proteccion_4490_2025.html")

    def test_returns_results(self, html):
        results = parse_search_results(html, "apelaciones")
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_jwt_keys_present(self, html):
        results = parse_search_results(html, "apelaciones")
        for m in results:
            assert m["key"].startswith("eyJ")

    def test_fields_present(self, html):
        results = parse_search_results(html, "apelaciones")
        for m in results:
            assert m["rol"]
            assert m["tribunal"]
            assert m["caratulado"]


class TestParseSearchPenal:
    @pytest.fixture
    def html(self):
        return _load("search_Penal_O_100_2025.html")

    def test_returns_results(self, html):
        results = parse_search_results(html, "penal")
        assert isinstance(results, list)
        assert len(results) == 1

    def test_jwt_keys_present(self, html):
        results = parse_search_results(html, "penal")
        for m in results:
            assert m["key"].startswith("eyJ")

    def test_fields_present(self, html):
        results = parse_search_results(html, "penal")
        for m in results:
            assert "key" in m and m["key"]
            assert "rol" in m and m["rol"]
            assert "tribunal" in m and m["tribunal"]
            assert "caratulado" in m and m["caratulado"]
            assert "fecha_ingreso" in m

    def test_rol_is_rit(self, html):
        results = parse_search_results(html, "penal")
        assert results[0]["rol"] == "O-100-2025"


class TestParseSearchNoResults:
    def test_empty_html_returns_empty(self):
        results = parse_search_results("<html><body></body></html>", "civil")
        assert results == []


class TestDetectBlocked:
    def test_normal_html_not_blocked(self):
        assert detect_blocked("<html><body><table></table></body></html>") is False

    def test_captcha_is_blocked(self):
        assert detect_blocked('<div class="g-recaptcha" data-sitekey="abc"></div>') is True

    def test_empty_response_is_blocked(self):
        assert detect_blocked("") is True

    def test_request_rejected_is_blocked(self):
        html = (
            "<html><head><title>Request Rejected</title></head>"
            "<body>The requested URL was rejected. Please consult with your "
            "administrator (2).<br>Your support ID is: <123></body></html>"
        )
        assert detect_blocked(html) is True
