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
            competencia="civil", tipo="C", numero="1234", anno="2024", corte=90,
        )
        assert form["conTipoCausa"] == "C"
        assert form["conCorte"] == "0"

    def test_canonical_non_appeals_maps_court_and_tribunal(self):
        form = build_search_form_data(
            competencia="civil",
            case_type="rol",
            case_number="C-1234-2024",
            corte=90,
            tribunal=123,
        )
        assert form["conCorte"] == "90"
        assert form["conTribunal"] == "123"

    def test_canonical_suprema_maps_explicit_none_filters_to_zero(self):
        form = build_search_form_data(
            competencia="suprema",
            case_type="rol",
            case_number="340-2025",
            corte=None,
            tribunal=None,
            search_mode="supreme_resource",
        )
        assert form["conCorte"] == "0"
        assert form["conTribunal"] == "0"

    def test_first_instance_broad_maps_explicit_none_filters_to_zero(self):
        form = build_search_form_data(
            competencia="apelaciones",
            case_type="rol",
            case_number="340-2025",
            corte=None,
            tribunal=None,
            libro="31",
            search_mode="first_instance",
            allow_broad=True,
        )
        assert form["conCorte"] == "0"
        assert form["conTribunal"] == "0"
        assert form["conTipoBusApe"] == "1"

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

    def test_apelaciones_numeric_libro_maps_to_pjud_text_value(self):
        form = build_search_form_data(
            competencia="apelaciones",
            tipo="PROTECCION",
            numero="7661",
            anno="2026",
            corte=46,
            libro="34",
        )

        assert form["conTipoCausa"] == "PROTECCION"
        assert form["conCorte"] == "46"

    def test_penal_ruc_populates_real_pjud_fields(self):
        form = build_search_form_data(
            competencia="penal",
            case_type="ruc",
            case_number="2400012345-6",
            corte=90,
            tribunal=123,
            libro="1",
        )
        assert form["radio-groupPenal"] == "2"
        assert form["rucPen1"] == "2400012345"
        assert form["rucPen2"] == "6"
        assert form["conRolCausa"] == ""
        assert form["conEraCausa"] == ""

    def test_appeals_first_instance_maps_all_filters(self):
        form = build_search_form_data(
            competencia="apelaciones",
            case_type="rol",
            case_number="340-2025",
            corte=90,
            tribunal=123,
            libro="31",
            search_mode="first_instance",
        )
        assert form["conCorte"] == "90"
        assert form["conTribunal"] == "123"
        assert form["conTipoBusApe"] == "1"
