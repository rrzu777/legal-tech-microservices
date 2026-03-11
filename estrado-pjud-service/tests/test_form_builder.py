import pytest
from app.parsers.form_builder import build_search_form_data


class TestBuildSearchFormData:
    """Test libro integration in form data builder."""

    @pytest.mark.parametrize("competencia,tipo,libro,expected_tipo_causa", [
        # Explicit libro overrides tipo
        ("civil", "C", "V", "V"),
        ("laboral", "O", "T", "T"),
        ("cobranza", "C", "J", "J"),
        # No libro: falls back to tipo
        ("civil", "C", None, "C"),
        ("laboral", "T", None, "T"),
        # No libro, no tipo: falls back to competencia default
        ("civil", "", None, "C"),
        ("laboral", "", None, "O"),
        ("cobranza", "", None, "C"),
    ])
    def test_con_tipo_causa_with_libro(self, competencia, tipo, libro, expected_tipo_causa):
        form = build_search_form_data(
            competencia=competencia, tipo=tipo, numero="1234", anno="2024", libro=libro,
        )
        assert form["conTipoCausa"] == expected_tipo_causa

    def test_suprema_uses_con_tipo_bus_ignores_libro(self):
        form = build_search_form_data(
            competencia="suprema", tipo="", numero="100", anno="2025", libro="X",
        )
        assert form["conTipoBus"] == "0"
        assert "conTipoCausa" not in form

    def test_suprema_without_libro(self):
        form = build_search_form_data(
            competencia="suprema", tipo="", numero="100", anno="2025",
        )
        assert form["conTipoBus"] == "0"
        assert "conTipoCausa" not in form

    def test_backwards_compatible_without_libro(self):
        """Calling without libro kwarg still works (backwards compat)."""
        form = build_search_form_data(
            competencia="civil", tipo="C", numero="1234", anno="2024",
        )
        assert form["conTipoCausa"] == "C"

    def test_unknown_libro_logs_warning(self, caplog):
        """Unknown libro value logs a warning but doesn't raise."""
        import logging
        with caplog.at_level(logging.WARNING, logger="app.parsers.form_builder"):
            form = build_search_form_data(
                competencia="civil", tipo="C", numero="1234", anno="2024", libro="Z",
            )
        assert form["conTipoCausa"] == "Z"  # still uses it
        assert "libro='Z' not in known values" in caplog.text

    def test_known_libro_no_warning(self, caplog):
        """Known libro value does not produce a warning."""
        import logging
        with caplog.at_level(logging.WARNING, logger="app.parsers.form_builder"):
            build_search_form_data(
                competencia="civil", tipo="C", numero="1234", anno="2024", libro="V",
            )
        assert "not in known values" not in caplog.text
