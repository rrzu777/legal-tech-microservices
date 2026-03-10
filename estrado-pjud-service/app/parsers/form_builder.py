"""Build OJV search form data — shared between API routes and worker engine."""

from app.parsers.normalizer import competencia_code


def build_search_form_data(
    competencia: str,
    tipo: str,
    numero: str,
    anno: str,
    corte: int | str = 0,
) -> dict[str, str]:
    """Build the form data dict for an OJV search request."""
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
        form_data["conTipoCausa"] = tipo

    return form_data
