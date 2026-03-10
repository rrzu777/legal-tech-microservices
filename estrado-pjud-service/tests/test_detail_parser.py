from pathlib import Path
import pytest

from app.parsers.detail_parser import parse_detail

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_bytes().decode("latin-1")


class TestParseDetailCivil:
    @pytest.fixture
    def result(self):
        html = _load("detail_Civil_C_1234_2024.html")
        return parse_detail(html)

    def test_metadata_has_required_fields(self, result):
        md = result["metadata"]
        assert md["rol"]
        assert md["tribunal"]
        assert md["estado_administrativo"]
        assert md["procedimiento"]
        assert md["estado_procesal"]
        assert md["etapa"]

    def test_metadata_values(self, result):
        md = result["metadata"]
        assert "C-1234-2024" in md["rol"]
        assert "Coquimbo" in md["tribunal"]
        assert md["estado_administrativo"] == "Sin archivar"

    def test_movements_is_list(self, result):
        assert isinstance(result["movements"], list)
        assert len(result["movements"]) >= 1

    def test_movement_fields(self, result):
        mov = result["movements"][0]
        assert "folio" in mov
        assert "etapa" in mov
        assert "tramite" in mov
        assert "descripcion" in mov
        assert "fecha" in mov

    def test_movement_date_is_iso(self, result):
        for mov in result["movements"]:
            if mov["fecha"]:
                assert len(mov["fecha"]) == 10
                assert mov["fecha"][4] == "-"

    def test_litigantes_is_list(self, result):
        assert isinstance(result["litigantes"], list)
        assert len(result["litigantes"]) >= 1

    def test_litigante_fields(self, result):
        lig = result["litigantes"][0]
        assert "rol" in lig
        assert "rut" in lig
        assert "nombre" in lig


class TestParseDetailSuprema:
    @pytest.fixture
    def result(self):
        html = _load("detail_Suprema_100_2025.html")
        return parse_detail(html)

    def test_movements_found(self, result):
        assert len(result["movements"]) >= 1

    def test_movement_has_fields(self, result):
        mov = result["movements"][0]
        assert "folio" in mov
        assert "tramite" in mov
        assert "fecha" in mov

    def test_litigantes_found(self, result):
        assert len(result["litigantes"]) >= 1

    def test_litigante_has_fields(self, result):
        lig = result["litigantes"][0]
        assert "rut" in lig
        assert "nombre" in lig

    def test_metadata_has_rol(self, result):
        assert result["metadata"]["rol"]

    def test_metadata_has_estado_procesal(self, result):
        assert result["metadata"]["estado_procesal"]


class TestParseDetailApelaciones:
    @pytest.fixture
    def result(self):
        html = _load("detail_Apelaciones_Proteccion_4490_2025.html")
        return parse_detail(html)

    def test_movements_found(self, result):
        assert len(result["movements"]) >= 1

    def test_movement_has_tramite(self, result):
        tramites = [m["tramite"] for m in result["movements"]]
        assert any(t for t in tramites)  # at least one non-empty

    def test_litigantes_found(self, result):
        assert len(result["litigantes"]) >= 1

    def test_metadata_has_rol(self, result):
        assert result["metadata"]["rol"]

    def test_metadata_has_estado_procesal(self, result):
        assert result["metadata"]["estado_procesal"]

    def test_metadata_has_tribunal(self, result):
        assert result["metadata"]["tribunal"]


class TestParseDetailEmpty:
    def test_empty_html(self):
        result = parse_detail("<html><body></body></html>")
        assert result["metadata"] == {}
        assert result["movements"] == []
        assert result["litigantes"] == []
