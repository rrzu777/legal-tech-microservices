"""Tests for the PJUD anexo modal HTML parser."""

from pathlib import Path

from app.parsers.anexo_parser import parse_anexo_list

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestParseBasic:
    """Parse the apelaciones sample fixture."""

    def test_parse_basic(self):
        html = _load_fixture("anexo_apelaciones_sample.html")
        results = parse_anexo_list(html)
        assert len(results) == 2

        # First document
        assert results[0]["download_url"] == "ADIR_871/apelaciones/documentos/anexoDocEscritoApelaciones.php"
        assert results[0]["download_token"] == "eyJhbGciOiJIUzI1NiJ9.eyJ0ZXN0IjoiYW5leG8xIn0.fake1"
        assert results[0]["download_param"] == "dtaDoc"

        # Second document
        assert results[1]["download_url"] == "ADIR_871/apelaciones/documentos/anexoDocEscritoApelaciones.php"
        assert results[1]["download_token"] == "eyJhbGciOiJIUzI1NiJ9.eyJ0ZXN0IjoiYW5leG8yIn0.fake2"
        assert results[1]["download_param"] == "dtaDoc"


class TestEdgeCases:
    """Edge cases: empty table, no table, missing form."""

    def test_parse_empty_table(self):
        html = '<table class="table"><thead><tr><th>Doc.</th></tr></thead><tbody></tbody></table>'
        results = parse_anexo_list(html)
        assert results == []

    def test_parse_no_table(self):
        html = "<div><p>No hay documentos</p></div>"
        results = parse_anexo_list(html)
        assert results == []

    def test_parse_missing_form(self):
        html = """
        <table>
          <tbody>
            <tr>
              <td>Sin formulario</td>
              <td>0-99</td>
              <td>Algo</td>
            </tr>
          </tbody>
        </table>
        """
        results = parse_anexo_list(html)
        assert results == []


class TestLabelExtraction:
    """Verify label and codigo extraction."""

    def test_parse_label_extraction(self):
        html = _load_fixture("anexo_apelaciones_sample.html")
        results = parse_anexo_list(html)
        # Label joins non-empty, non-"0" columns after the first td
        # Row 1: "0-43", "Mandato", "1", "" → "0-43 — Mandato — 1"
        assert results[0]["label"] == "0-43 — Mandato — 1"
        # Row 2: "0-44", "Poder", "1", "Documento notarial" → "0-44 — Poder — 1 — Documento notarial"
        assert results[1]["label"] == "0-44 — Poder — 1 — Documento notarial"

    def test_parse_codigo(self):
        html = _load_fixture("anexo_apelaciones_sample.html")
        results = parse_anexo_list(html)
        assert results[0]["codigo"] == "0-43"
        assert results[1]["codigo"] == "0-44"
