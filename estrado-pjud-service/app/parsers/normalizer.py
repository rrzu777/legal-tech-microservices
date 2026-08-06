import logging
import re
from datetime import date

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^(.+)-(\d+)-(\d{4})$")
_IDENTIFIER_NUM_RE = re.compile(r"^(\d+)-(\d{4})$")
_RUC_IDENTIFIER_RE = re.compile(r"^(\d{7,10})-([0-9Kk])$")
_DATE_DMY_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_DATE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_COMPETENCIA_CODES = {"suprema": 1, "apelaciones": 2, "civil": 3, "laboral": 4, "penal": 5, "cobranza": 6}

VALID_LIBROS: dict[str, set[str]] = {
    "civil": {"C", "V", "E", "A", "I"},
    "laboral": {"O", "T", "M", "E", "I", "S"},
    "cobranza": {"A", "C", "D", "E", "J", "P", "R"},
}

APELACIONES_LIBRO_CODE_MAP: dict[str, str] = {
    "28": "CIVIL",
    "29": "FAMILIA",
    "30": "LABORAL",
    "31": "PENAL",
    "32": "CONTENCIOSO",
    "33": "TRIBUTARIO",
    "34": "PROTECCION",
    "35": "AMPARO",
    "36": "POLICIA",
    "37": "EXHORTO",
    "38": "NAVEGACION",
    "39": "AMBIENTAL",
    "40": "TRASPASO",
    "41": "MINISTRO",
    "42": "COMLIBCOND",
}

LIBRO_DEFAULTS: dict[str, str] = {
    "civil": "C",
    "laboral": "O",
    "cobranza": "C",
}


def resolve_libro(competencia: str, tipo: str, libro: str | None = None) -> str:
    """Resolve the effective libro (conTipoCausa) value.

    Fallback chain: explicit libro -> extracted tipo -> competencia default.
    Returns "" for suprema (uses conTipoBus instead).
    """
    if competencia == "suprema":
        return ""
    if libro:
        if competencia == "apelaciones":
            return APELACIONES_LIBRO_CODE_MAP.get(libro, libro)
        return libro
    if tipo:
        return tipo
    return LIBRO_DEFAULTS.get(competencia, "")


def parse_case_identifier(raw: str) -> dict[str, str]:
    raw = raw.strip()
    m = _IDENTIFIER_RE.match(raw)
    if m:
        return {"tipo": m.group(1).upper(), "numero": m.group(2), "anno": m.group(3)}
    m = _IDENTIFIER_NUM_RE.match(raw)
    if m:
        return {"tipo": "", "numero": m.group(1), "anno": m.group(2)}
    raise ValueError(f"Invalid case identifier: {raw!r}")


def parse_search_identifier(case_type: str, raw: str) -> dict[str, str | None]:
    """Parse the canonical v2 identifier without rewriting PJUD prefixes."""
    if case_type == "ruc":
        match = _RUC_IDENTIFIER_RE.fullmatch(raw.strip())
        if not match:
            raise ValueError("Invalid RUC identifier")
        return {
            "tipo": "",
            "numero": "",
            "anno": "",
            "ruc": match.group(1),
            "ruc_dv": match.group(2).upper(),
        }

    return {**parse_case_identifier(raw), "ruc": None, "ruc_dv": None}


def normalize_date(raw: str | None) -> str | None:
    """Normaliza a ISO (YYYY-MM-DD). Devuelve None si no es una fecha valida.

    Devolver el string crudo cuando no parseaba era una bomba de tiempo: el worker
    manda esto a una columna `date` de Postgres, asi que cualquier corrimiento del
    mapa de columnas del parser terminaba en `invalid input syntax for type date`
    (22007) y, a los 10 intentos, en una causa suspendida. Le paso a C-1000-2024,
    que recibio "Firmado" — un valor de la columna estado.

    Descartar la fecha pierde un dato; devolver basura pierde la causa entera.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None

    if _DATE_ISO_RE.match(raw):
        try:
            date.fromisoformat(raw)
        except ValueError:
            logger.warning("Fecha ISO imposible, se descarta: %r", raw)
            return None
        return raw

    m = _DATE_DMY_RE.match(raw)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            logger.warning("Fecha DMY imposible, se descarta: %r", raw)
            return None

    logger.warning("Valor no reconocido como fecha, se descarta: %r", raw)
    return None


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
