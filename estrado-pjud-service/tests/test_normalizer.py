import pytest

from app.parsers.normalizer import (
    parse_case_identifier,
    normalize_date,
    competencia_code,
    competencia_path,
)


class TestParseCaseIdentifier:
    def test_civil_case(self):
        result = parse_case_identifier("C-1234-2024")
        assert result == {"tipo": "C", "numero": "1234", "anno": "2024"}

    def test_labor_case(self):
        result = parse_case_identifier("T-500-2024")
        assert result == {"tipo": "T", "numero": "500", "anno": "2024"}

    def test_lowercase(self):
        result = parse_case_identifier("c-1234-2024")
        assert result["tipo"] == "C"

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid case identifier"):
            parse_case_identifier("INVALID")

    def test_missing_year(self):
        with pytest.raises(ValueError, match="Invalid case identifier"):
            parse_case_identifier("C-1234")

    def test_multi_char_tipo(self):
        result = parse_case_identifier("Proteccion-4490-2025")
        assert result == {"tipo": "PROTECCION", "numero": "4490", "anno": "2025"}

    def test_number_only_suprema(self):
        result = parse_case_identifier("100-2025")
        assert result == {"tipo": "", "numero": "100", "anno": "2025"}


class TestNormalizeDate:
    def test_dd_mm_yyyy(self):
        assert normalize_date("31/05/2024") == "2024-05-31"

    def test_already_iso(self):
        assert normalize_date("2024-05-31") == "2024-05-31"

    def test_empty(self):
        assert normalize_date("") is None

    def test_none(self):
        assert normalize_date(None) is None

    def test_with_spaces(self):
        assert normalize_date("  31/05/2024  ") == "2024-05-31"


class TestCompetenciaCodes:
    def test_civil(self):
        assert competencia_code("civil") == 3

    def test_laboral(self):
        assert competencia_code("laboral") == 4

    def test_cobranza(self):
        assert competencia_code("cobranza") == 6

    def test_suprema(self):
        assert competencia_code("suprema") == 1

    def test_apelaciones(self):
        assert competencia_code("apelaciones") == 2

    def test_penal(self):
        assert competencia_code("penal") == 5

    def test_unknown(self):
        with pytest.raises(ValueError):
            competencia_code("familia")


class TestCompetenciaPath:
    def test_civil(self):
        assert competencia_path("civil") == "civil"

    def test_laboral(self):
        assert competencia_path("laboral") == "laboral"

    def test_cobranza(self):
        assert competencia_path("cobranza") == "cobranza"

    def test_suprema(self):
        assert competencia_path("suprema") == "suprema"

    def test_apelaciones(self):
        assert competencia_path("apelaciones") == "apelaciones"

    def test_penal(self):
        assert competencia_path("penal") == "penal"
