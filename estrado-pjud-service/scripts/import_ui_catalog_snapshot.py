"""Import one reviewed, secret-free PJUD UI capture into the runtime snapshot."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping


SOURCE_URL = "https://oficinajudicialvirtual.pjud.cl/indexN.php"
CAPTURED_AT = "2026-08-06T05:01:52.325Z"
HISTORICAL_APPEALS_AT = "2026-03-12T00:23:37+00:00"
COURT_CODES = (
    "10", "11", "15", "20", "25", "30", "35", "40", "45", "46",
    "50", "55", "56", "60", "61", "90", "91",
)
COMPETENCIAS = ("apelaciones", "civil", "laboral", "penal", "cobranza")
YEARS = range(2022, 2027)
APPEALS_BOOKS = (
    ("28", "Civil"), ("29", "Familia"), ("30", "Laboral - Cobranza"),
    ("31", "Penal"), ("32", "Contencioso Administrativo"),
    ("33", "Tributario y Aduanero"), ("34", "Protección"), ("35", "Amparo"),
    ("36", "Policía Local"), ("37", "Exhorto"), ("38", "Ley de Navegación"),
    ("39", "Ambiental"), ("40", "Traspaso Corte Marcial"),
    ("41", "Ministro 1ª Instancia y Fuero"), ("42", "Com. Lib. Cond."),
)
OFFICIAL_BOOKS = {
    "civil": (("C", "C"), ("V", "V"), ("E", "E"), ("A", "A"), ("F", "F"), ("I", "I")),
    "laboral": (("O", "O"), ("T", "T"), ("M", "M"), ("E", "E"), ("S", "S"), ("U", "U"), ("V", "V"), ("I", "I")),
    "penal": (("1", "Ordinaria"), ("2", "Exhorto"), ("3", "Administrativa"), ("4", "Extradición"), ("5", "Militar")),
    "cobranza": (("A", "A"), ("C", "C"), ("D", "D"), ("E", "E"), ("J", "J"), ("L", "L"), ("P", "P"), ("R", "R")),
}
SENTINEL_CODES = {"", "0", "-1"}
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:authorization|bearer|cookie|jwt|token|password|secret|api[ _-]?key|"
    r"proxy(?:[ _-]?url)?|(?:support|request|incident|ticket)[ _-]?(?:id|case))",
    re.IGNORECASE,
)
SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?:authorization|bearer|cookie|jwt|\btoken\b|\bpassword\b|\bsecret\b|"
    r"api[ _-]?key|proxy(?:[ _-]?url)?|(?:support|request|incident|ticket)[ _-]?(?:id|case))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReviewedCapture:
    captured_at: str
    courts: list[dict[str, str]]
    tribunals: dict[tuple[str, str], list[dict[str, str]]]
    books: dict[tuple[str, str], list[dict[str, str]]]


def _fail(message: str) -> None:
    """Raise a safe error that never contains capture data."""
    raise ValueError(message)


def _assert_no_sensitive_data(value: Any) -> None:
    """Reject obvious secrets or support identifiers anywhere in a capture."""
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            if not isinstance(key, str) or SENSITIVE_KEY_PATTERN.search(key):
                _fail("UI capture contains forbidden metadata")
            _assert_no_sensitive_data(nested_value)
    elif isinstance(value, list):
        for nested_value in value:
            _assert_no_sensitive_data(nested_value)
    elif isinstance(value, str) and SENSITIVE_VALUE_PATTERN.search(value):
        _fail("UI capture contains forbidden metadata")


def _options(value: Any, context: str) -> list[dict[str, str]]:
    """Validate source rows before normalizing their display labels."""
    if not isinstance(value, list):
        _fail(f"UI capture has invalid {context} options")

    options: list[dict[str, str]] = []
    seen_codes: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"code", "label"}:
            _fail(f"UI capture has invalid {context} option")
        code, raw_label = item["code"], item["label"]
        if not isinstance(code, str) or code != code.strip() or code in SENTINEL_CODES:
            _fail(f"UI capture has invalid {context} code")
        if not isinstance(raw_label, str):
            _fail(f"UI capture has invalid {context} label")
        label = " ".join(raw_label.split())
        if not label or label.casefold().startswith("seleccione"):
            _fail(f"UI capture has invalid {context} label")
        if code in seen_codes:
            _fail(f"UI capture has duplicate {context} code")
        seen_codes.add(code)
        options.append({"code": code, "label": label})
    return options


def _capture_time(value: Any) -> str:
    if not isinstance(value, str):
        _fail("UI capture has invalid captured_at")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("UI capture has invalid captured_at")
    if value != CAPTURED_AT:
        _fail("UI capture is not the reviewed capture")
    return value


def validate_capture(value: Any) -> ReviewedCapture:
    """Return only a complete, reviewed catalog capture or fail closed."""
    _assert_no_sensitive_data(value)
    if not isinstance(value, Mapping) or set(value) != {
        "source_url", "captured_at", "courts", "competencias",
    }:
        _fail("UI capture has unexpected structure")
    if value["source_url"] != SOURCE_URL:
        _fail("UI capture has an untrusted source")

    captured_at = _capture_time(value["captured_at"])
    courts = _options(value["courts"], "court")
    court_codes = [court["code"] for court in courts]
    if set(court_codes) != set(COURT_CODES) or len(court_codes) != len(COURT_CODES):
        _fail("UI capture does not contain the official courts")

    competencias = value["competencias"]
    if not isinstance(competencias, Mapping) or set(competencias) != set(COMPETENCIAS):
        _fail("UI capture does not contain the supported competencias")

    tribunals: dict[tuple[str, str], list[dict[str, str]]] = {}
    books: dict[tuple[str, str], list[dict[str, str]]] = {}
    for competencia in COMPETENCIAS:
        entries = competencias[competencia]
        if not isinstance(entries, Mapping) or set(entries) != set(COURT_CODES):
            _fail("UI capture has incomplete competencia courts")
        reference_books: list[dict[str, str]] | None = None
        for court in COURT_CODES:
            entry = entries[court]
            if not isinstance(entry, Mapping) or set(entry) != {
                "court_label", "tribunal_disabled", "book_disabled", "tribunals", "books",
            }:
                _fail("UI capture has invalid competencia entry")
            court_label = entry["court_label"]
            if not isinstance(court_label, str) or not " ".join(court_label.split()):
                _fail("UI capture has invalid competencia court label")
            if not isinstance(entry["tribunal_disabled"], bool) or not isinstance(entry["book_disabled"], bool):
                _fail("UI capture has invalid competencia state")
            tribunal_options = _options(entry["tribunals"], "tribunal")
            if not tribunal_options:
                _fail("UI capture has empty tribunal options")
            tribunals[(competencia, court)] = tribunal_options

            if competencia == "apelaciones":
                if not isinstance(entry["books"], list):
                    _fail("UI capture has invalid appeals books")
                continue

            book_options = _options(entry["books"], "book")
            official_books = [
                {"code": code, "label": label} for code, label in OFFICIAL_BOOKS[competencia]
            ]
            if book_options != official_books:
                _fail("UI capture has non-official books")
            if reference_books is None:
                reference_books = book_options
            elif book_options != reference_books:
                _fail("UI capture has inconsistent books between courts")
            books[(competencia, court)] = book_options

    if len(tribunals) != 85 or sum(len(options) for options in tribunals.values()) != 1865:
        _fail("UI capture has incomplete tribunal coverage")
    return ReviewedCapture(captured_at, courts, tribunals, books)


def _record(options: list[dict[str, str]], fetched_at: str) -> dict[str, Any]:
    return {"fetched_at": fetched_at, "options": options}


def build_snapshot(capture: ReviewedCapture) -> dict[str, Any]:
    """Build the entire validated runtime snapshot in memory."""
    snapshot: dict[str, Any] = {
        "generated_at": capture.captured_at,
        "courts": {"1": _record(capture.courts, capture.captured_at)},
        "tribunals": {},
        "books": {},
    }
    versioned_appeals = [{"code": code, "label": label} for code, label in APPEALS_BOOKS]
    for competencia in COMPETENCIAS:
        timestamp = HISTORICAL_APPEALS_AT if competencia == "apelaciones" else capture.captured_at
        first_court_books: list[dict[str, str]] | None = None
        for court in COURT_CODES:
            snapshot["tribunals"][f"{competencia}:{court}:1"] = _record(
                capture.tribunals[(competencia, court)], capture.captured_at
            )
            book_options = versioned_appeals if competencia == "apelaciones" else capture.books[(competencia, court)]
            if first_court_books is None:
                first_court_books = book_options
            for year in YEARS:
                snapshot["books"][f"{competencia}:{court}:{year}"] = _record(book_options, timestamp)
        assert first_court_books is not None
        for year in YEARS:
            snapshot["books"][f"{competencia}::{year}"] = _record(first_court_books, timestamp)

    if (
        len(snapshot["tribunals"]) != 85
        or sum(len(item["options"]) for item in snapshot["tribunals"].values()) != 1865
        or len(snapshot["books"]) != 450
    ):
        _fail("snapshot generation is incomplete")
    return snapshot


def write_snapshot(snapshot: Mapping[str, Any], output: Path) -> None:
    """Atomically replace output only after capture validation and construction finish."""
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent,
            prefix=f".{output.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary.write(payload)
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, output)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def main(source: Path, output: Path) -> None:
    try:
        raw_capture = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("unable to read UI capture") from error
    write_snapshot(build_snapshot(validate_capture(raw_capture)), output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    main(arguments.source, arguments.output)
