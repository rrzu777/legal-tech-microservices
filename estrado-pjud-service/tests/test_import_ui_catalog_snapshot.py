"""Safety contract for the reviewed PJUD UI catalog importer."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts import import_ui_catalog_snapshot as importer


COURT_CODES = (
    "10", "11", "15", "20", "25", "30", "35", "40", "45", "46",
    "50", "55", "56", "60", "61", "90", "91",
)
BOOKS = {
    "civil": (("C", "C"), ("V", "V"), ("E", "E"), ("A", "A"), ("F", "F"), ("I", "I")),
    "laboral": (("O", "O"), ("T", "T"), ("M", "M"), ("E", "E"), ("S", "S"), ("U", "U"), ("V", "V"), ("I", "I")),
    "penal": (("1", "Ordinaria"), ("2", "Exhorto"), ("3", "Administrativa"), ("4", "Extradición"), ("5", "Militar")),
    "cobranza": (("A", "A"), ("C", "C"), ("D", "D"), ("E", "E"), ("J", "J"), ("L", "L"), ("P", "P"), ("R", "R")),
}
CAPTURE_PATH = Path("/tmp/juristrack-pjud-ui-catalog-2026-08-06.json")


def _capture() -> dict:
    """A full-shape fixture: 17 courts, 85 tribunal keys and 1,865 options."""
    competencias = {}
    for competencia in ("apelaciones", "civil", "laboral", "penal", "cobranza"):
        courts = {}
        for index, code in enumerate(COURT_CODES):
            count = 22 if (len(competencias) * len(COURT_CODES) + index) < 80 else 21
            courts[code] = {
                "court_label": f"Corte {code}",
                "tribunal_disabled": False,
                "book_disabled": competencia == "apelaciones",
                "tribunals": [
                    {"code": f"{competencia[:2]}-{code}-{option}", "label": f"Tribunal {code} {option}"}
                    for option in range(count)
                ],
                # Apelaciones is deliberately ignored in favour of the versioned list.
                "books": (
                    [{"code": "raw", "label": "Raw appeals value"}]
                    if competencia == "apelaciones"
                    else [{"code": code, "label": label} for code, label in BOOKS[competencia]]
                ),
            }
        competencias[competencia] = courts

    return {
        "source_url": "https://oficinajudicialvirtual.pjud.cl/indexN.php",
        "captured_at": "2026-08-06T05:01:52.325Z",
        "courts": [{"code": code, "label": f"Corte {code}"} for code in COURT_CODES],
        "competencias": competencias,
    }


def _import(capture: dict, tmp_path: Path, *, output_contents: str = "unchanged") -> Path:
    source = tmp_path / "capture.json"
    output = tmp_path / "catalog_snapshot.json"
    source.write_text(json.dumps(capture), encoding="utf-8")
    output.write_text(output_contents, encoding="utf-8")
    importer.main(source, output)
    return output


def _assert_rejected_without_writing(capture: dict, tmp_path: Path) -> None:
    output = tmp_path / "catalog_snapshot.json"
    with pytest.raises(ValueError):
        _import(capture, tmp_path)
    assert output.read_text(encoding="utf-8") == "unchanged"


def test_importer_builds_the_complete_catalog_in_memory_before_writing(tmp_path):
    """Catches a partially-written snapshot from a validation failure."""
    output = _import(_capture(), tmp_path)
    snapshot = json.loads(output.read_text(encoding="utf-8"))

    assert len(snapshot["tribunals"]) == 85
    assert sum(len(item["options"]) for item in snapshot["tribunals"].values()) == 1865
    assert len(snapshot["books"]) == 450
    assert snapshot["books"]["apelaciones:10:2022"] == {
        "fetched_at": "2026-03-12T00:23:37+00:00",
        "options": [
            {"code": "28", "label": "Civil"},
            {"code": "29", "label": "Familia"},
            {"code": "30", "label": "Laboral - Cobranza"},
            {"code": "31", "label": "Penal"},
            {"code": "32", "label": "Contencioso Administrativo"},
            {"code": "33", "label": "Tributario y Aduanero"},
            {"code": "34", "label": "Protección"},
            {"code": "35", "label": "Amparo"},
            {"code": "36", "label": "Policía Local"},
            {"code": "37", "label": "Exhorto"},
            {"code": "38", "label": "Ley de Navegación"},
            {"code": "39", "label": "Ambiental"},
            {"code": "40", "label": "Traspaso Corte Marcial"},
            {"code": "41", "label": "Ministro 1ª Instancia y Fuero"},
            {"code": "42", "label": "Com. Lib. Cond."},
        ],
    }
    assert snapshot["books"]["civil:10:2022"]["fetched_at"] == "2026-08-06T05:01:52.325Z"


def test_importer_rejects_a_non_official_source_url_without_writing(tmp_path):
    """Catches accepting a capture copied from an arbitrary host."""
    capture = _capture()
    capture["source_url"] = "https://example.invalid/indexN.php"

    _assert_rejected_without_writing(capture, tmp_path)


def test_importer_rejects_an_invalid_or_unreviewed_capture_time_without_writing(tmp_path):
    """Catches treating any timestamp string as the reviewed UI capture."""
    capture = _capture()
    capture["captured_at"] = "not-an-iso-timestamp"

    _assert_rejected_without_writing(capture, tmp_path)


def test_importer_rejects_replacing_an_official_court_code_without_writing(tmp_path):
    """Catches a 10 -> 999 substitution that preserves the old count of 17."""
    capture = _capture()
    capture["courts"][0]["code"] = "999"
    for competencia in capture["competencias"].values():
        competencia["999"] = competencia.pop("10")

    _assert_rejected_without_writing(capture, tmp_path)


@pytest.mark.parametrize("invalid_court", [
    {"code": "10", "label": "Corte 10"},
    {"code": "0", "label": "Seleccione una corte"},
])
def test_importer_rejects_duplicate_or_sentinel_courts_without_writing(tmp_path, invalid_court):
    """Catches clean() silently hiding an adulterated court list."""
    capture = _capture()
    capture["courts"].append(invalid_court)

    _assert_rejected_without_writing(capture, tmp_path)


def test_importer_rejects_an_extra_competencia_without_writing(tmp_path):
    """Catches a capture with a sixth competencia outside the supported contract."""
    capture = _capture()
    capture["competencias"]["suprema"] = copy.deepcopy(capture["competencias"]["civil"])

    _assert_rejected_without_writing(capture, tmp_path)


def test_importer_rejects_tribunals_truncated_to_one_per_combination_without_writing(tmp_path):
    """Catches the old non-empty-only tribunal validation."""
    capture = _capture()
    for competencia in capture["competencias"].values():
        for court in competencia.values():
            court["tribunals"] = court["tribunals"][:1]

    _assert_rejected_without_writing(capture, tmp_path)


@pytest.mark.parametrize("bad_tribunal", [
    {"code": "ci-10-0", "label": "Tribunal 10 0"},
    {"code": "0", "label": "Seleccione un tribunal"},
])
def test_importer_rejects_duplicate_or_sentinel_tribunals_without_writing(tmp_path, bad_tribunal):
    """Catches filtering invalid tribunal rows rather than rejecting the capture."""
    capture = _capture()
    tribunals = capture["competencias"]["civil"]["10"]["tribunals"]
    tribunals.append(bad_tribunal)

    _assert_rejected_without_writing(capture, tmp_path)


@pytest.mark.parametrize("books", [[], [{"code": "Z", "label": "Invented book"}]])
def test_importer_rejects_empty_or_non_official_books_without_writing(tmp_path, books):
    """Catches accepting missing or fabricated first-instance book sets."""
    capture = _capture()
    for court in capture["competencias"]["civil"].values():
        court["books"] = books

    _assert_rejected_without_writing(capture, tmp_path)


def test_importer_rejects_sensitive_capture_metadata_without_echoing_it(tmp_path):
    """Catches accidentally versioning session/support data with the snapshot."""
    capture = _capture()
    secret = "do-not-persist-this-token"
    capture["session_token"] = secret

    with pytest.raises(ValueError) as error:
        _import(capture, tmp_path)

    assert secret not in str(error.value)
    assert (tmp_path / "catalog_snapshot.json").read_text(encoding="utf-8") == "unchanged"


@pytest.mark.skipif(not CAPTURE_PATH.exists(), reason="reviewed UI capture is supplied outside the repository")
def test_reviewed_ui_capture_reproduces_the_versioned_snapshot_byte_for_byte(tmp_path):
    """Catches an importer change that drifts from the reviewed production snapshot."""
    output = tmp_path / "catalog_snapshot.json"
    importer.main(CAPTURE_PATH, output)

    assert output.read_bytes() == (Path(__file__).parents[1] / "app/catalog_snapshot.json").read_bytes()
