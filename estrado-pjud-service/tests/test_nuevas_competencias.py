"""Tests para nuevas competencias PJUD (suprema, apelaciones, penal)."""

import pytest
from app.parsers.normalizer import competencia_code, competencia_path
from app.parsers.search_parser import (
    detect_auth_redirect,
    AuthenticationRequired,
    _parse_suprema_row,
    _parse_apelaciones_row,
    _parse_penal_row,
)


class TestCompetenciaCodes:
    """Test competencia codes y paths."""

    def test_suprema_code(self):
        assert competencia_code("suprema") == 1

    def test_apelaciones_code(self):
        assert competencia_code("apelaciones") == 2

    def test_penal_code(self):
        assert competencia_code("penal") == 5

    def test_suprema_path(self):
        assert competencia_path("suprema") == "suprema"

    def test_apelaciones_path(self):
        assert competencia_path("apelaciones") == "apelaciones"

    def test_penal_path(self):
        assert competencia_path("penal") == "penal"

    def test_invalid_competencia(self):
        with pytest.raises(ValueError, match="Unknown competencia"):
            competencia_code("invalida")


class TestAuthRedirect:
    """Test detección de auth redirects."""

    def test_detect_auth_redirect_with_index_php(self):
        html = "<script>parent.window.open('../../index.php','_self');</script>"
        assert detect_auth_redirect(html) is True

    def test_detect_auth_redirect_with_parent_window(self):
        html = "<script>parent.window.open('index.php');</script>"
        assert detect_auth_redirect(html) is True

    def test_no_auth_redirect_normal_html(self):
        html = "<div>Normal search results</div>"
        assert detect_auth_redirect(html) is False


class TestSupremaParser:
    """Test parser para Suprema."""

    def test_parse_suprema_row(self):
        from bs4 import BeautifulSoup
        
        # JWT completo (3 partes como en producción)
        html = """
        <tr>
            <td><a onclick="detalleCausaSuprema('eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJwaWp1ZCIsImV4cCI6MTc3MjUwMTEzNX0.abc123def456')">🔍</a></td>
            <td>C-26903-2025</td>
            <td>31/05/2024</td>
            <td>Fundación Orden vs Municipalidad de Santiago</td>
            <td>Corte Suprema</td>
        </tr>
        """
        soup = BeautifulSoup(html, "html.parser")
        tr = soup.find("tr")
        
        result = _parse_suprema_row(tr)
        
        assert result is not None
        assert result["key"] == "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJwaWp1ZCIsImV4cCI6MTc3MjUwMTEzNX0.abc123def456"
        assert result["rol"] == "C-26903-2025"
        assert result["fecha_ingreso"] == "2024-05-31"
        assert "Fundación Orden" in result["caratulado"]
        assert "Corte Suprema" in result["tribunal"]


class TestApelacionesParser:
    """Test parser para Apelaciones."""

    def test_parse_apelaciones_row(self):
        from bs4 import BeautifulSoup
        
        html = """
        <tr>
            <td><a onclick="detalleCausaApelaciones('eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJwaWp1ZCIsImV4cCI6MTc3MjUwMTEzNX0.abc123def456')">🔍</a></td>
            <td>Proteccion-4490-2024</td>
            <td>15/03/2024</td>
            <td>Juan Pérez vs Banco Estado</td>
            <td>Corte de Apelaciones de Santiago</td>
        </tr>
        """
        soup = BeautifulSoup(html, "html.parser")
        tr = soup.find("tr")
        
        result = _parse_apelaciones_row(tr)
        
        assert result is not None
        assert result["key"] == "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJwaWp1ZCIsImV4cCI6MTc3MjUwMTEzNX0.abc123def456"
        assert result["rol"] == "Proteccion-4490-2024"
        assert result["fecha_ingreso"] == "2024-03-15"


class TestPenalParser:
    """Test parser para Penal."""

    def test_parse_penal_row(self):
        from bs4 import BeautifulSoup
        
        html = """
        <tr>
            <td><a onclick="detalleCausaPenal('eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJwaWp1ZCIsImV4cCI6MTc3MjUwMTEzNX0.abc123def456')">🔍</a></td>
            <td>O-1234-2024</td>
            <td>2401510892-0</td>
            <td>Juzgado de Garantía de Santiago</td>
            <td>Ministerio Público vs Juan Pérez</td>
            <td>10/01/2024</td>
        </tr>
        """
        soup = BeautifulSoup(html, "html.parser")
        tr = soup.find("tr")
        
        result = _parse_penal_row(tr)
        
        assert result is not None
        assert result["key"] == "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJwaWp1ZCIsImV4cCI6MTc3MjUwMTEzNX0.abc123def456"
        assert result["rol"] == "O-1234-2024"
        assert result["ruc"] == "2401510892-0"
        assert result["fecha_ingreso"] == "2024-01-10"
