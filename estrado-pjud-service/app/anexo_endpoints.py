"""Map PJUD JS function names to their AJAX endpoint + POST param."""

# (endpoint_path_relative_to_ADIR_871, POST_param_name)
ANEXO_ENDPOINTS: dict[str, tuple[str, str]] = {
    # Apelaciones
    "anexoEscritoApelaciones": ("apelaciones/modal/anexoEscritoApelaciones.php", "dtaAnexEsc"),
    "anexoRecursoApelaciones": ("apelaciones/modal/anexoRecursoApelaciones.php", "dtaAnexRec"),
    "anexoCausaApelaciones": ("apelaciones/modal/anexoCausaApelaciones.php", "dtaAnexCau"),
    # Civil
    "anexoSolicitudCivil": ("civil/modal/anexoCausaSolicitudCivil.php", "dtaCausaAnex"),
    "anexoCausaCivil": ("civil/modal/anexoCausaCivil.php", "dtaAnexCau"),
    "anexoSolicitudCivilSII": ("civil/modal/anexoCausaSolicitudCivilSII.php", "dtaCausaAnex"),
    "anexoSolicitudCivilEscrit": ("civil/modal/anexoCausaSolEscritoCivil.php", "dtaCausaAnexSol"),
    # Laboral
    "anexoEscritoLaboral": ("laboral/modal/anexoEscritoLaboral.php", "dtaAnex"),
    "anexoEscPendLaboral": ("laboral/modal/anexoEscPendLaboral.php", "dtaTxtDem"),
    # Cobranza
    "anexoCausaCobranza": ("cobranza/modal/anexoCausaCobranza.php", "dtaAnexCau"),
    "anexoEscritoCobranza": ("cobranza/modal/anexoEscritoCobranza.php", "dtaAnexEsc"),
    "anexoRequieraseCobranza": ("cobranza/modal/anexoRequieraseCobranza.php", "dtaRequierase"),
    "anexoOficieseCobranza": ("cobranza/modal/anexoOficieseCobranza.php", "dtaOficiese"),
    "anexoArrestoCobranza": ("cobranza/modal/anexoArrestoCobranza.php", "dtaOficiese"),
    # Penal
    "anexoDemandaPenal": ("penal/modal/anexoDemandaPenal.php", "dtaAnex"),
    "anexoEscritoPenal": ("penal/modal/anexoEscritoPenal.php", "dtaAnex"),
    "anexoDemandaPenalUnificado": ("unificado/modal/anexoDemandaUnificado.php", "dtaAnex"),
    "anexoEscritoPenalUnificado": ("unificado/modal/anexoEscritoUnificado.php", "dtaAnex"),
}
