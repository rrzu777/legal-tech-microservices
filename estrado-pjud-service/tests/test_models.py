import pytest
from pydantic import ValidationError
from app.models import DetailRequest, SearchRequest, SearchResponse


class TestCanonicalSearchRequestV2:
    @pytest.mark.parametrize("request_type", [SearchRequest, DetailRequest])
    def test_match_window_defaults_to_10_accepts_100_and_rejects_101(self, request_type):
        payload = {
            "contract_version": 2,
            "case_type": "rol",
            "case_number": "C-561-2025",
            "competencia": "civil",
            "libro": "C",
            "allow_broad": True,
        }
        if request_type is DetailRequest:
            payload["detail_key"] = "key"

        assert request_type(**payload).max_matches == 10
        assert request_type(**payload, max_matches=100).max_matches == 100
        with pytest.raises(ValidationError):
            request_type(**payload, max_matches=101)

    @pytest.mark.parametrize("competencia,case_type,case_number,extra", [
        ("civil", "rol", "C-1234-2024", {"corte": 90, "tribunal": 321, "libro": "F"}),
        ("laboral", "rit", "O-1234-2024", {"corte": 90, "tribunal": 321, "libro": "U"}),
        ("penal", "rit", "O-243-2025", {"corte": 90, "tribunal": 321, "libro": "5"}),
        ("cobranza", "rol", "C-1234-2024", {"corte": 90, "tribunal": 321, "libro": "L"}),
        ("apelaciones", "rol", "4490-2025", {"corte": 90, "libro": "42", "search_mode": "appeals_resource"}),
    ])
    def test_v2_accepts_observed_official_book_codes(self, competencia, case_type, case_number, extra):
        request = SearchRequest(
            contract_version=2,
            competencia=competencia,
            case_type=case_type,
            case_number=case_number,
            **extra,
        )
        assert request.libro == extra["libro"]

    @pytest.mark.parametrize("request_type", [SearchRequest, DetailRequest])
    def test_v2_rejects_unknown_official_book_code(self, request_type):
        payload = {
            "contract_version": 2,
            "competencia": "apelaciones",
            "case_type": "rol",
            "case_number": "4490-2025",
            "corte": 90,
            "libro": "999",
            "search_mode": "appeals_resource",
        }
        if request_type is DetailRequest:
            payload["detail_key"] = "key"
        with pytest.raises(ValidationError):
            request_type(**payload)

    def test_v2_civil_requires_court_and_tribunal_unless_broad(self):
        with pytest.raises(ValidationError):
            SearchRequest(
                contract_version=2,
                case_type="rol",
                case_number="C-1-2026",
                competencia="civil",
            )

        req = SearchRequest(
            contract_version=2,
            case_type="rol",
            case_number="C-1-2026",
            competencia="civil",
            allow_broad=True,
        )

        assert req.corte is None
        assert req.tribunal is None

    def test_v2_civil_rejects_partial_or_broad_filters(self):
        with pytest.raises(ValidationError):
            SearchRequest(
                contract_version=2,
                case_type="rol",
                case_number="C-1-2026",
                competencia="civil",
                corte=90,
            )
        with pytest.raises(ValidationError):
            SearchRequest(
                contract_version=2,
                case_type="rol",
                case_number="C-1-2026",
                competencia="civil",
                corte=90,
                tribunal=123,
                allow_broad=True,
            )

    def test_v2_appeals_modes_validate_dependent_fields(self):
        direct = SearchRequest(
            contract_version=2,
            case_type="rol",
            case_number="340-2025",
            competencia="apelaciones",
            corte=90,
            libro="31",
            search_mode="appeals_resource",
        )
        assert direct.tribunal is None

        origin = SearchRequest(
            contract_version=2,
            case_type="rol",
            case_number="340-2025",
            competencia="apelaciones",
            corte=90,
            tribunal=1234,
            search_mode="first_instance",
        )
        assert origin.tribunal == 1234

    @pytest.mark.parametrize("request_type", [SearchRequest, DetailRequest])
    def test_v2_first_instance_rejects_libro(self, request_type):
        fields = {
            "contract_version": 2,
            "case_type": "rol",
            "case_number": "340-2025",
            "competencia": "apelaciones",
            "corte": 90,
            "tribunal": 1234,
            "libro": "31",
            "search_mode": "first_instance",
        }
        if request_type is DetailRequest:
            fields["detail_key"] = "key"

        with pytest.raises(ValidationError):
            request_type(**fields)

    def test_v2_appeals_rejects_invalid_mode_fields(self):
        with pytest.raises(ValidationError):
            SearchRequest(
                contract_version=2,
                case_type="rol",
                case_number="340-2025",
                competencia="apelaciones",
                corte=90,
                libro="31",
                search_mode="appeals_resource",
                tribunal=1234,
            )
        with pytest.raises(ValidationError):
            SearchRequest(
                contract_version=2,
                case_type="rol",
                case_number="340-2025",
                competencia="apelaciones",
                corte=90,
                libro="31",
                search_mode="first_instance",
            )

    @pytest.mark.parametrize("search_mode,tribunal", [
        ("appeals_resource", None),
        ("first_instance", 1234),
    ])
    def test_v2_appeals_rejects_v1_all_courts_sentinel(self, search_mode, tribunal):
        with pytest.raises(ValidationError):
            SearchRequest(
                contract_version=2,
                case_type="rol",
                case_number="340-2025",
                competencia="apelaciones",
                corte=0,
                tribunal=tribunal,
                libro="31",
                search_mode=search_mode,
            )

    def test_v2_supreme_accepts_only_supreme_resource_fields(self):
        req = SearchRequest(
            contract_version=2,
            case_type="rol",
            case_number="340-2025",
            competencia="suprema",
            search_mode="supreme_resource",
        )
        assert req.search_mode == "supreme_resource"

        with pytest.raises(ValidationError):
            SearchRequest(
                contract_version=2,
                case_type="rol",
                case_number="340-2025",
                competencia="suprema",
                search_mode="supreme_resource",
                libro="31",
            )

    def test_v2_penal_accepts_rit_and_ruc_only(self):
        rit = SearchRequest(
            contract_version=2,
            case_type="rit",
            case_number="O-243-2025",
            competencia="penal",
            corte=90,
            tribunal=123,
            libro="1",
        )
        ruc = SearchRequest(
            contract_version=2,
            case_type="ruc",
            case_number="2400012345-6",
            competencia="penal",
            corte=90,
            tribunal=123,
        )
        assert rit.case_type == "rit"
        assert ruc.case_type == "ruc"

        with pytest.raises(ValidationError):
            SearchRequest(
                contract_version=2,
                case_type="ruc",
                case_number="2400012345-6",
                competencia="civil",
                corte=90,
                tribunal=123,
            )

    def test_v2_penal_rejects_noncanonical_rit_and_ruc_identifiers(self):
        with pytest.raises(ValidationError):
            SearchRequest(
                contract_version=2,
                case_type="rit",
                case_number="243-2025",
                competencia="penal",
                corte=90,
                tribunal=123,
                libro="1",
            )
        with pytest.raises(ValidationError):
            SearchRequest(
                contract_version=2,
                case_type="ruc",
                case_number="not-a-ruc",
                competencia="penal",
                corte=90,
                tribunal=123,
            )

    def test_v1_request_remains_accepted_during_rollout(self):
        req = SearchRequest(
            case_type="rit",
            case_number="O-243-2025",
            competencia="penal",
            libro="1",
        )
        assert req.contract_version == 1

    def test_v1_keeps_legacy_case_type_values(self):
        req = SearchRequest(
            case_type="legacy-cause-type",
            case_number="O-243-2025",
            competencia="penal",
            libro="1",
        )
        assert req.case_type == "legacy-cause-type"

    @pytest.mark.parametrize(("competencia", "case_type", "expected_case_type"), [
        ("cobranza", "rit", "rit"),
        ("cobranza", "rol", "rit"),
        ("civil", "rol", "rol"),
        ("laboral", "rit", "rit"),
        ("penal", "ruc", "ruc"),
    ])
    def test_cobranza_uses_rit_without_rewriting_other_competencias(
        self, competencia, case_type, expected_case_type,
    ):
        """Changing this normalization must not rewrite non-Cobranza requests."""
        request = SearchRequest(
            competencia=competencia,
            case_type=case_type,
            case_number="C-1234-2025",
        )

        assert request.case_type == expected_case_type
        assert request.model_dump()["case_type"] == expected_case_type

    @pytest.mark.parametrize("contract_version, extra", [
        (1, {}),
        (2, {"corte": 90, "tribunal": 321, "libro": "C"}),
    ])
    def test_cobranza_rejects_ruc_for_each_contract_version(self, contract_version, extra):
        """Cobranza never has an RUC search path, unlike Penal."""
        with pytest.raises(ValidationError, match="cobranza requires rit"):
            SearchRequest(
                contract_version=contract_version,
                competencia="cobranza",
                case_type="ruc",
                case_number="2400012345-6",
                **extra,
            )

    def test_v2_rejects_legacy_case_type_values(self):
        with pytest.raises(ValidationError):
            SearchRequest(
                contract_version=2,
                case_type="legacy-cause-type",
                case_number="C-1-2026",
                competencia="civil",
                corte=90,
                tribunal=123,
            )

    @pytest.mark.parametrize("request_type", [SearchRequest, DetailRequest])
    @pytest.mark.parametrize("competencia,case_type,case_number,extra", [
        (
            "apelaciones",
            "rol",
            "340-2025",
            {"corte": 90, "search_mode": "appeals_resource"},
        ),
        (
            "penal",
            "rit",
            "O-243-2025",
            {"corte": 90, "tribunal": 123},
        ),
    ])
    @pytest.mark.parametrize("libro", ["", " \t "])
    def test_v2_rejects_empty_or_whitespace_required_libro(
        self, request_type, competencia, case_type, case_number, extra, libro,
    ):
        fields = {
            "contract_version": 2,
            "case_type": case_type,
            "case_number": case_number,
            "competencia": competencia,
            "libro": libro,
            **extra,
        }
        if request_type is DetailRequest:
            fields["detail_key"] = "key"

        with pytest.raises(ValidationError):
            request_type(**fields)

    @pytest.mark.parametrize("request_type", [SearchRequest, DetailRequest])
    def test_v2_strips_nonempty_libro_before_form_consumers(self, request_type):
        fields = {
            "contract_version": 2,
            "case_type": "rol",
            "case_number": "340-2025",
            "competencia": "apelaciones",
            "corte": 90,
            "libro": " 31 ",
            "search_mode": "appeals_resource",
        }
        if request_type is DetailRequest:
            fields["detail_key"] = "key"

        req = request_type(**fields)
        assert req.libro == "31"

    def test_detail_request_carries_canonical_search_fields(self):
        req = DetailRequest(
            detail_key="key",
            contract_version=2,
            case_type="rol",
            case_number="340-2025",
            competencia="apelaciones",
            corte=90,
            tribunal=123,
            search_mode="first_instance",
            max_matches=25,
        )
        assert req.tribunal == 123
        assert req.max_matches == 25

    def test_detail_v2_rejects_invalid_canonical_combinations(self):
        with pytest.raises(ValidationError):
            DetailRequest(detail_key="key", contract_version=2)
        with pytest.raises(ValidationError):
            DetailRequest(
                detail_key="key",
                contract_version=2,
                case_type="rol",
                case_number="340-2025",
                competencia="apelaciones",
                corte=0,
                libro="31",
                search_mode="appeals_resource",
            )
        with pytest.raises(ValidationError):
            DetailRequest(
                detail_key="key",
                contract_version=2,
                case_type="rol",
                case_number="C-1-2026",
                competencia="civil",
                corte=90,
                tribunal=-1,
            )
        with pytest.raises(ValidationError):
            DetailRequest(
                detail_key="key",
                contract_version=2,
                case_type="rol",
                case_number="340-2025",
                competencia="suprema",
                corte=90,
                search_mode="supreme_resource",
            )


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


class TestSearchRequestLibro:
    def test_libro_optional_defaults_none(self):
        req = SearchRequest(case_type="rol", case_number="C-1234-2024", competencia="civil")
        assert req.libro is None

    def test_libro_accepted_when_provided(self):
        req = SearchRequest(case_type="rol", case_number="C-1234-2024", competencia="civil", libro="V")
        assert req.libro == "V"

    def test_libro_accepted_for_laboral(self):
        req = SearchRequest(case_type="rit", case_number="T-500-2024", competencia="laboral", libro="T")
        assert req.libro == "T"

    def test_libro_accepted_for_suprema(self):
        """Libro is accepted in the model even for suprema (ignored at form-builder level)."""
        req = SearchRequest(case_type="rol", case_number="100-2025", competencia="suprema", libro="X")
        assert req.libro == "X"


class TestSearchResponseLibroUsed:
    def test_libro_used_optional_defaults_none(self):
        resp = SearchResponse(found=False, match_count=0, matches=[], blocked=False, error=None)
        assert resp.libro_used is None

    def test_libro_used_accepted_when_provided(self):
        resp = SearchResponse(found=True, match_count=1, matches=[], blocked=False, error=None, libro_used="V")
        assert resp.libro_used == "V"
