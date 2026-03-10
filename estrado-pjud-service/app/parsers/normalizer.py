import re

_IDENTIFIER_RE = re.compile(r"^([A-Za-z]+)-(\d+)-(\d{4})$")
_IDENTIFIER_NUM_RE = re.compile(r"^(\d+)-(\d{4})$")
_DATE_DMY_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
_DATE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_COMPETENCIA_CODES = {"suprema": 1, "apelaciones": 2, "civil": 3, "laboral": 4, "penal": 5, "cobranza": 6}


def parse_case_identifier(raw: str) -> dict[str, str]:
    raw = raw.strip()
    m = _IDENTIFIER_RE.match(raw)
    if m:
        return {"tipo": m.group(1).upper(), "numero": m.group(2), "anno": m.group(3)}
    m = _IDENTIFIER_NUM_RE.match(raw)
    if m:
        return {"tipo": "", "numero": m.group(1), "anno": m.group(2)}
    raise ValueError(f"Invalid case identifier: {raw!r}")


def normalize_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if _DATE_ISO_RE.match(raw):
        return raw
    m = _DATE_DMY_RE.match(raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return raw


def competencia_code(competencia: str) -> int:
    c = competencia.lower()
    if c not in _COMPETENCIA_CODES:
        raise ValueError(f"Unknown competencia: {competencia!r}")
    return _COMPETENCIA_CODES[c]


def competencia_path(competencia: str) -> str:
    c = competencia.lower()
    if c not in _COMPETENCIA_CODES:
        raise ValueError(f"Unknown competencia: {competencia!r}")
    return c
