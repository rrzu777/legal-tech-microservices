"""Build OJV search form data — shared between API routes and worker engine."""

import logging

from app.parsers.normalizer import competencia_code, resolve_libro, VALID_LIBROS

logger = logging.getLogger(__name__)


def build_search_form_data(
    competencia: str,
    tipo: str,
    numero: str,
    anno: str,
    corte: int | str = 0,
    libro: str | None = None,
) -> dict[str, str]:
    """Build the form data dict for an OJV search request."""
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
        "conCorte": str(corte) if competencia == "apelaciones" else "0",
        "conTribunal": "0",
        "conTipoBusApe": "0",
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

    if competencia == "suprema":
        form_data["conTipoBus"] = "0"
    else:
        form_data["conTipoCausa"] = effective_libro

    return form_data
