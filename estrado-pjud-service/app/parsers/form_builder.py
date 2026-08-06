"""Build OJV search form data — shared between API routes and worker engine."""

import logging

from app.parsers.normalizer import (
    VALID_LIBROS,
    competencia_code,
    parse_search_identifier,
    resolve_libro,
)

logger = logging.getLogger(__name__)


def build_search_form_data(
    competencia: str,
    tipo: str | None = None,
    numero: str | None = None,
    anno: str | None = None,
    corte: int | str | None = 0,
    libro: str | None = None,
    *,
    case_type: str | None = None,
    case_number: str | None = None,
    tribunal: int | str | None = None,
    search_mode: str | None = None,
    allow_broad: bool = False,
) -> dict[str, str]:
    """Build OJV form data for legacy v1 or canonical v2 search inputs."""
    effective_case_type = case_type or "rol"
    is_canonical_input = case_number is not None
    parsed: dict[str, str | None] | None = None
    if is_canonical_input:
        parsed = parse_search_identifier(effective_case_type, case_number)
        tipo = parsed["tipo"]
        numero = parsed["numero"]
        anno = parsed["anno"]
    else:
        tipo = tipo or ""
        numero = numero or ""
        anno = anno or ""

    effective_libro = resolve_libro(competencia, tipo, libro)

    # Soft validation: warn if libro is not in known set
    if libro and competencia in VALID_LIBROS and libro not in VALID_LIBROS[competencia]:
        logger.warning(
            "libro=%r not in known values for %s: %s",
            libro, competencia, sorted(VALID_LIBROS[competencia]),
        )

    form_data = {
        "g-recaptcha-response-rit": "",
        "action": "validate_captcha_rit",
        "competencia": str(competencia_code(competencia)),
        "conCorte": str(corte) if corte is not None and (competencia == "apelaciones" or is_canonical_input) else "0",
        "conTribunal": str(tribunal) if tribunal is not None else "0",
        "conTipoBusApe": "1" if search_mode == "first_instance" else "0",
        "radio-groupPenal": "1",
        "radio-group": "1",
        "conRolCausa": numero,
        "conEraCausa": anno,
        "ruc1": "",
        "ruc2": "",
        "rucPen1": "",
        "rucPen2": "",
        "conCaratulado": "",
    }

    if competencia == "penal" and effective_case_type == "ruc":
        if parsed is None or parsed["ruc"] is None or parsed["ruc_dv"] is None:
            raise ValueError("RUC searches require case_number")
        form_data.update({
            "radio-groupPenal": "2",
            "conRolCausa": "",
            "conEraCausa": "",
            "rucPen1": parsed["ruc"],
            "rucPen2": parsed["ruc_dv"],
        })
    elif competencia == "suprema":
        form_data["conTipoBus"] = "0"
    else:
        form_data["conTipoCausa"] = effective_libro

    return form_data
