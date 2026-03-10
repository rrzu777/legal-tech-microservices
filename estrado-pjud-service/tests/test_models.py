import pytest
from pydantic import ValidationError
from app.models import SearchRequest


class TestSearchRequestCorte:
    def test_corte_valid_for_apelaciones(self):
        req = SearchRequest(case_type="rol", case_number="Proteccion-4490-2025", competencia="apelaciones", corte=90)
        assert req.corte == 90

    def test_corte_valid_santiago(self):
        req = SearchRequest(case_type="rol", case_number="Proteccion-4490-2025", competencia="apelaciones", corte=90)
        assert req.corte == 90

    def test_corte_valid_san_miguel(self):
        req = SearchRequest(case_type="rol", case_number="Proteccion-4490-2025", competencia="apelaciones", corte=91)
        assert req.corte == 91

    def test_corte_invalid_code_rejected(self):
        with pytest.raises(ValidationError, match="Invalid corte code"):
            SearchRequest(case_type="rol", case_number="Proteccion-4490-2025", competencia="apelaciones", corte=99)

    def test_corte_defaults_to_zero_for_apelaciones(self):
        req = SearchRequest(case_type="rol", case_number="Proteccion-4490-2025", competencia="apelaciones")
        assert req.corte == 0

    def test_corte_rejected_for_civil(self):
        with pytest.raises(ValidationError, match="corte"):
            SearchRequest(case_type="rol", case_number="C-1234-2024", competencia="civil", corte=90)

    def test_corte_rejected_for_penal(self):
        with pytest.raises(ValidationError, match="corte"):
            SearchRequest(case_type="rit", case_number="O-500-2024", competencia="penal", corte=90)

    def test_corte_none_for_non_apelaciones(self):
        req = SearchRequest(case_type="rol", case_number="C-1234-2024", competencia="civil")
        assert req.corte is None
