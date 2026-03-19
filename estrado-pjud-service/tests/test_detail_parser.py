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

    def test_libro_extracted_from_rol_prefix(self, result):
        """Libro is extracted from the ROL prefix (e.g. 'C' from 'C-1234-2024')."""
        assert result["metadata"]["libro"] == "C"

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

    def test_libro_extracted_from_libro_label(self, result):
        """Suprema libro is extracted from the 'Libro :' label."""
        assert result["metadata"].get("libro")


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


class TestParseDetailPenal:
    @pytest.fixture
    def result(self):
        html = _load("detail_Penal_O_100_2025.html")
        return parse_detail(html)

    def test_metadata_has_rit(self, result):
        assert "O-100-2025" in result["metadata"]["rol"]

    def test_metadata_has_ruc(self, result):
        assert result["metadata"]["ruc"]
        assert "2500100001-5" in result["metadata"]["ruc"]

    def test_movements_not_empty(self, result):
        assert len(result["movements"]) >= 1

    def test_movement_fields(self, result):
        for mov in result["movements"]:
            assert "folio" in mov
            assert "etapa" in mov
            assert "tramite" in mov
            assert "descripcion" in mov
            assert "fecha" in mov

    def test_intervinientes_not_empty(self, result):
        assert len(result["litigantes"]) >= 1

    def test_interviniente_fields(self, result):
        for lig in result["litigantes"]:
            assert "rol" in lig
            assert "rut" in lig
            assert "nombre" in lig


class TestDocumentoToken:
    def test_extracts_documento_token_from_form(self):
        """Movements with document forms should include the dtaDoc JWT."""
        html = _load("detail_Civil_C_1234_2024.html")
        result = parse_detail(html)
        docs_with_token = [m for m in result["movements"] if m.get("documento_token")]
        assert len(docs_with_token) > 0, "Should extract at least one documento_token"
        for m in docs_with_token:
            assert m["documento_token"].startswith("eyJ"), "Token should be a JWT (starts with eyJ)"
            assert m["documento_url"] is not None, "Should also have documento_url"

    def test_movement_without_document_has_none_token(self):
        """Movements without a document form should have None token."""
        html = _load("detail_Civil_C_1234_2024.html")
        result = parse_detail(html)
        docs_without = [m for m in result["movements"] if m.get("documento_url") is None]
        for m in docs_without:
            assert m.get("documento_token") is None


class TestDocumentosAdicionales:
    """Tests for additional document extraction (certificates) from the Doc column."""

    def test_civil_folio_14_has_certificate(self):
        """Civil folio 14 has main doc (docuN.php/dtaDoc) + certificate (docCertificadoEscrito.php/dtaCert)."""
        html = _load("detail_Civil_C_1234_2024.html")
        result = parse_detail(html)
        folio_14 = [m for m in result["movements"] if m["folio"] == 14]
        assert len(folio_14) == 1
        mov = folio_14[0]
        # Primary document should be the main doc
        assert mov["documento_url"] is not None
        assert "docuN.php" in mov["documento_url"]
        assert mov["documento_param"] == "dtaDoc"
        # Should have one additional document (certificate)
        assert len(mov["documentos_adicionales"]) == 1
        cert = mov["documentos_adicionales"][0]
        assert "docCertificadoEscrito.php" in cert["url"]
        assert cert["param"] == "dtaCert"
        assert cert["token"].startswith("eyJ")

    def test_civil_folio_15_no_additional_docs(self):
        """Civil folio 15 has only one form (docuS.php), no additional docs."""
        html = _load("detail_Civil_C_1234_2024.html")
        result = parse_detail(html)
        folio_15 = [m for m in result["movements"] if m["folio"] == 15]
        assert len(folio_15) == 1
        assert folio_15[0]["documentos_adicionales"] == []

    def test_apelaciones_folio_8_has_certificate(self):
        """Apelaciones folio 8 has main doc + certificate form."""
        html = _load("detail_Apelaciones_Proteccion_4490_2025.html")
        result = parse_detail(html)
        folio_8 = [m for m in result["movements"] if m["folio"] == 8]
        assert len(folio_8) == 1
        mov = folio_8[0]
        assert mov["documento_url"] is not None
        assert mov["documento_param"] == "valorDoc"
        assert len(mov["documentos_adicionales"]) == 1
        cert = mov["documentos_adicionales"][0]
        assert "docCertificadoEscrito.php" in cert["url"]
        assert cert["param"] == "dtaCert"

    def test_apelaciones_folio_6_has_certificate(self):
        """Apelaciones folio 6 also has main doc + certificate form."""
        html = _load("detail_Apelaciones_Proteccion_4490_2025.html")
        result = parse_detail(html)
        folio_6 = [m for m in result["movements"] if m["folio"] == 6]
        assert len(folio_6) == 1
        mov = folio_6[0]
        assert len(mov["documentos_adicionales"]) == 1


class TestAnexoToken:
    """Tests for anexo JWT token extraction from the Anexo column."""

    def test_apelaciones_folio_8_has_anexo(self):
        """Apelaciones folio 8 has an anexo link with JWT token."""
        html = _load("detail_Apelaciones_Proteccion_4490_2025.html")
        result = parse_detail(html)
        folio_8 = [m for m in result["movements"] if m["folio"] == 8]
        assert len(folio_8) == 1
        assert folio_8[0]["anexo_token"] is not None
        assert folio_8[0]["anexo_token"].startswith("eyJ")

    def test_apelaciones_folio_6_has_anexo(self):
        """Apelaciones folio 6 has an anexo link with JWT token."""
        html = _load("detail_Apelaciones_Proteccion_4490_2025.html")
        result = parse_detail(html)
        folio_6 = [m for m in result["movements"] if m["folio"] == 6]
        assert len(folio_6) == 1
        assert folio_6[0]["anexo_token"] is not None
        assert folio_6[0]["anexo_token"].startswith("eyJ")

    def test_apelaciones_folio_11_no_anexo(self):
        """Apelaciones folio 11 has no anexo (empty td)."""
        html = _load("detail_Apelaciones_Proteccion_4490_2025.html")
        result = parse_detail(html)
        folio_11 = [m for m in result["movements"] if m["folio"] == 11]
        assert len(folio_11) == 1
        assert folio_11[0]["anexo_token"] is None

    def test_civil_folio_10_has_anexo(self):
        """Civil folio 10 has an anexo solicitud link."""
        html = _load("detail_Civil_C_1234_2024.html")
        result = parse_detail(html)
        folio_10 = [m for m in result["movements"] if m["folio"] == 10]
        assert len(folio_10) == 1
        assert folio_10[0]["anexo_token"] is not None
        assert folio_10[0]["anexo_token"].startswith("eyJ")

    def test_civil_folio_15_no_anexo(self):
        """Civil folio 15 has no anexo."""
        html = _load("detail_Civil_C_1234_2024.html")
        result = parse_detail(html)
        folio_15 = [m for m in result["movements"] if m["folio"] == 15]
        assert len(folio_15) == 1
        assert folio_15[0]["anexo_token"] is None

    def test_suprema_no_anexos(self):
        """Suprema fixture has no anexo links in movements."""
        html = _load("detail_Suprema_100_2025.html")
        result = parse_detail(html)
        for mov in result["movements"]:
            assert mov["anexo_token"] is None

    def test_penal_no_anexos(self):
        """Penal fixture has no anexo links in movements."""
        html = _load("detail_Penal_O_100_2025.html")
        result = parse_detail(html)
        for mov in result["movements"]:
            assert mov["anexo_token"] is None


class TestAnexoFunc:
    """Tests for anexo JS function name extraction."""

    def test_apelaciones_folio_8_has_func(self):
        """Apelaciones folio 8 should capture anexoEscritoApelaciones."""
        html = _load("detail_Apelaciones_Proteccion_4490_2025.html")
        result = parse_detail(html)
        folio_8 = [m for m in result["movements"] if m["folio"] == 8]
        assert len(folio_8) == 1
        assert folio_8[0]["anexo_func"] == "anexoEscritoApelaciones"

    def test_civil_folio_10_has_func(self):
        """Civil folio 10 should capture anexoSolicitudCivil."""
        html = _load("detail_Civil_C_1234_2024.html")
        result = parse_detail(html)
        folio_10 = [m for m in result["movements"] if m["folio"] == 10]
        assert len(folio_10) == 1
        assert folio_10[0]["anexo_func"] == "anexoSolicitudCivil"

    def test_movement_without_anexo_has_none_func(self):
        """Movements without anexos should have anexo_func=None."""
        html = _load("detail_Apelaciones_Proteccion_4490_2025.html")
        result = parse_detail(html)
        folio_11 = [m for m in result["movements"] if m["folio"] == 11]
        assert len(folio_11) == 1
        assert folio_11[0]["anexo_func"] is None

    def test_suprema_no_anexo_func(self):
        """Suprema fixture has no anexo functions."""
        html = _load("detail_Suprema_100_2025.html")
        result = parse_detail(html)
        for mov in result["movements"]:
            assert mov["anexo_func"] is None

    def test_penal_no_anexo_func(self):
        """Penal fixture has no anexo functions."""
        html = _load("detail_Penal_O_100_2025.html")
        result = parse_detail(html)
        for mov in result["movements"]:
            assert mov["anexo_func"] is None


class TestBackwardCompatibility:
    """Ensure new fields don't break existing behavior."""

    def test_all_movements_have_new_fields(self):
        """Every movement dict should include documentos_adicionales, anexo_token, and anexo_func."""
        html = _load("detail_Civil_C_1234_2024.html")
        result = parse_detail(html)
        for mov in result["movements"]:
            assert "documentos_adicionales" in mov
            assert "anexo_token" in mov
            assert "anexo_func" in mov
            assert isinstance(mov["documentos_adicionales"], list)

    def test_primary_doc_unchanged_civil(self):
        """Primary document extraction still works the same for civil."""
        html = _load("detail_Civil_C_1234_2024.html")
        result = parse_detail(html)
        docs_with_token = [m for m in result["movements"] if m.get("documento_token")]
        assert len(docs_with_token) > 0
        for m in docs_with_token:
            assert m["documento_token"].startswith("eyJ")
            assert m["documento_url"] is not None
            assert m["documento_param"] in ("dtaDoc", "valorDoc", "valorFile")

    def test_primary_doc_unchanged_apelaciones(self):
        """Primary document extraction still works for apelaciones (valorDoc param)."""
        html = _load("detail_Apelaciones_Proteccion_4490_2025.html")
        result = parse_detail(html)
        docs_with_token = [m for m in result["movements"] if m.get("documento_token")]
        assert len(docs_with_token) > 0
        for m in docs_with_token:
            assert m["documento_param"] == "valorDoc"

    def test_primary_doc_unchanged_suprema(self):
        """Primary document extraction works for suprema (valorFile param)."""
        html = _load("detail_Suprema_100_2025.html")
        result = parse_detail(html)
        docs_with_token = [m for m in result["movements"] if m.get("documento_token")]
        assert len(docs_with_token) > 0
        for m in docs_with_token:
            assert m["documento_param"] == "valorFile"


class TestParseDetailEmpty:
    def test_empty_html(self):
        result = parse_detail("<html><body></body></html>")
        assert result["metadata"] == {}
        assert result["movements"] == []
        assert result["litigantes"] == []
