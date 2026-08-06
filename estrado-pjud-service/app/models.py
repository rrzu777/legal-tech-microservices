import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

COMPETENCIA_TYPE = Literal["suprema", "apelaciones", "civil", "laboral", "penal", "cobranza"]
SearchMode = Literal["supreme_resource", "appeals_resource", "first_instance"]
SearchStatus = Literal[
    "found",
    "not_found",
    "needs_disambiguation",
    "pjud_blocked",
    "pjud_timeout",
    "upstream_changed",
]

_NUMERO_ANNO_RE = re.compile(r"^\d+-\d{4}$")
_RIT_IDENTIFIER_RE = re.compile(r"^[^-]+-\d+-\d{4}$")
_RUC_IDENTIFIER_RE = re.compile(r"^\d{7,10}-[0-9Kk]$")

_V2_LIBROS: dict[str, set[str]] = {
    "civil": {"C", "V", "E", "A", "F", "I"},
    "laboral": {"O", "T", "M", "E", "S", "U", "V", "I"},
    "penal": {"1", "2", "3", "4", "5"},
    "cobranza": {"A", "C", "D", "E", "J", "L", "P", "R"},
    "apelaciones": {str(code) for code in range(28, 43)},
}


VALID_CORTE_CODES = {
    0,   # Todas (all courts)
    10,  # C.A. de Arica
    11,  # C.A. de Iquique
    15,  # C.A. de Antofagasta
    20,  # C.A. de Copiapó
    25,  # C.A. de La Serena
    30,  # C.A. de Valparaíso
    35,  # C.A. de Rancagua
    40,  # C.A. de Talca
    45,  # C.A. de Chillán
    46,  # C.A. de Concepción
    50,  # C.A. de Temuco
    55,  # C.A. de Valdivia
    56,  # C.A. de Puerto Montt
    60,  # C.A. de Coyhaique
    61,  # C.A. de Punta Arenas
    90,  # C.A. de Santiago
    91,  # C.A. de San Miguel
}


def _validate_v2_search_contract(
    *,
    case_type: str,
    case_number: str,
    competencia: COMPETENCIA_TYPE,
    corte: int | None,
    tribunal: int | None,
    libro: str | None,
    search_mode: SearchMode | None,
    allow_broad: bool,
) -> str | None:
    if libro is not None:
        libro = libro.strip()
        if not libro:
            raise ValueError("v2 libro must not be empty or whitespace")
        allowed_libros = _V2_LIBROS.get(competencia)
        if allowed_libros is not None and libro not in allowed_libros:
            raise ValueError(
                f"Invalid v2 libro {libro!r} for {competencia}; must be one of {sorted(allowed_libros)}"
            )
    if case_type not in {"rol", "rit", "ruc"}:
        raise ValueError("v2 case_type must be rol, rit, or ruc")
    if corte is not None and (corte == 0 or corte not in VALID_CORTE_CODES):
        raise ValueError(
            f"Invalid v2 corte code {corte}; must be a real court code from {sorted(VALID_CORTE_CODES - {0})}"
        )
    if tribunal is not None and tribunal <= 0:
        raise ValueError("tribunal must be a positive integer")

    if competencia == "suprema":
        if case_type != "rol" or search_mode != "supreme_resource":
            raise ValueError("v2 suprema requires rol with search_mode='supreme_resource'")
        if not _NUMERO_ANNO_RE.fullmatch(case_number):
            raise ValueError("v2 suprema requires case_number numero-año")
        if any(value is not None for value in (corte, tribunal, libro)) or allow_broad:
            raise ValueError("v2 suprema does not accept corte, tribunal, libro, or allow_broad")
        return libro

    if competencia == "apelaciones":
        if case_type != "rol" or not _NUMERO_ANNO_RE.fullmatch(case_number):
            raise ValueError("v2 apelaciones requires rol with case_number numero-año")
        if corte is None:
            raise ValueError("v2 apelaciones requires corte")
        if search_mode not in {"appeals_resource", "first_instance"}:
            raise ValueError("v2 apelaciones requires a valid search_mode")
        if search_mode == "appeals_resource":
            if libro is None:
                raise ValueError("appeals_resource requires libro")
            if tribunal is not None or allow_broad:
                raise ValueError("appeals_resource does not accept tribunal or allow_broad")
        else:
            if libro is not None:
                raise ValueError("first_instance does not accept libro")
            if tribunal is None and not allow_broad:
                raise ValueError("first_instance requires tribunal unless allow_broad is true")
        return libro

    if search_mode is not None:
        raise ValueError("search_mode is only valid for suprema and apelaciones")
    if competencia == "penal":
        if case_type not in {"rit", "ruc"}:
            raise ValueError("v2 penal requires rit or ruc")
        if case_type == "rit" and not _RIT_IDENTIFIER_RE.fullmatch(case_number.strip()):
            raise ValueError("v2 penal RIT requires prefijo-numero-año")
        if case_type == "ruc" and not _RUC_IDENTIFIER_RE.fullmatch(case_number.strip()):
            raise ValueError("v2 penal RUC requires digitos-DV")
        if case_type == "ruc" and libro is not None:
            raise ValueError("v2 penal RUC does not accept libro")
        if case_type == "rit" and libro is None:
            raise ValueError("v2 penal RIT requires libro")
    elif case_type == "ruc":
        raise ValueError("v2 civil, laboral, and cobranza do not accept ruc")

    if allow_broad:
        if corte is not None or tribunal is not None:
            raise ValueError("allow_broad requires corte and tribunal to be omitted")
    elif corte is None or tribunal is None:
        raise ValueError("v2 searches require corte and tribunal unless allow_broad is true")
    return libro


class SearchRequest(BaseModel):
    contract_version: Literal[1, 2] = 1
    case_type: str
    case_number: str  # "X-NNNN-YYYY"
    competencia: COMPETENCIA_TYPE
    corte: int | None = None
    tribunal: int | None = None
    libro: str | None = None
    search_mode: SearchMode | None = None
    allow_broad: bool = False
    max_matches: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def _validate_corte(self):
        if self.contract_version == 2:
            return self._validate_v2()

        if self.corte is not None and self.competencia != "apelaciones":
            raise ValueError("corte is only valid when competencia is 'apelaciones'")
        if self.competencia == "apelaciones" and self.corte is None:
            self.corte = 0
        if self.corte is not None and self.corte not in VALID_CORTE_CODES:
            raise ValueError(
                f"Invalid corte code {self.corte}; must be one of {sorted(VALID_CORTE_CODES)}"
            )
        return self

    def _validate_v2(self):
        self.libro = _validate_v2_search_contract(
            case_type=self.case_type,
            case_number=self.case_number,
            competencia=self.competencia,
            corte=self.corte,
            tribunal=self.tribunal,
            libro=self.libro,
            search_mode=self.search_mode,
            allow_broad=self.allow_broad,
        )
        return self


class CandidateMatch(BaseModel):
    key: str
    rol: str
    ruc: str | None = None
    tribunal: str
    caratulado: str
    fecha_ingreso: str | None
    # PJUD's result rows usually expose labels, not stable catalog codes.  The
    # search route enriches these only when the official catalog has one unique
    # normalized match; values are never guessed from display text.
    tribunal_code: int | None = None
    corte: str | None = None
    corte_code: int | None = None
    libro: str | None = None
    libro_code: str | None = None


class SearchResponse(BaseModel):
    found: bool
    match_count: int
    matches: list[CandidateMatch]
    blocked: bool
    error: str | None
    libro_used: str | None = None
    # Additive v2 rollout fields.  The legacy quartet above keeps its exact
    # semantics until JurisTrack has switched to the canonical contract.
    status: SearchStatus = "not_found"
    truncated: bool = False


class DetailRequest(BaseModel):
    detail_key: str
    contract_version: Literal[1, 2] = 1
    case_type: str | None = None
    competencia: COMPETENCIA_TYPE | None = None
    # Optional search params: when provided, the detail endpoint performs a search
    # on the SAME session before fetching the detail, ensuring JWT + CSRF affinity.
    # This prevents cross-case contamination when session pooling reuses sessions.
    case_number: str | None = None
    corte: int | None = None
    tribunal: int | None = None
    libro: str | None = None
    search_mode: SearchMode | None = None
    allow_broad: bool = False
    max_matches: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def _validate_v2_search_contract(self):
        if self.contract_version == 1:
            return self
        if self.case_type is None or self.case_number is None or self.competencia is None:
            raise ValueError("v2 detail requires case_type, case_number, and competencia")
        self.libro = _validate_v2_search_contract(
            case_type=self.case_type,
            case_number=self.case_number,
            competencia=self.competencia,
            corte=self.corte,
            tribunal=self.tribunal,
            libro=self.libro,
            search_mode=self.search_mode,
            allow_broad=self.allow_broad,
        )
        return self


class CaseMetadata(BaseModel):
    rol: str = ""
    tribunal: str = ""
    estado_administrativo: str = ""
    procedimiento: str = ""
    estado_procesal: str = ""
    etapa: str = ""
    libro: str = ""  # extracted from ROL/RIT prefix or Libro label
    # Competencia-specific fields
    ruc: str = ""           # penal
    ubicacion: str = ""     # suprema, apelaciones
    fecha: str = ""         # suprema, apelaciones
    caratulado: str = ""    # suprema
    tipo: str = ""          # suprema
    recurso: str = ""       # apelaciones
    # Observación tab fields (suprema)
    naturaleza_recurso: str = ""
    numero_oficio: str = ""
    abogado_suspendido: str = ""
    tabla: str = ""


class DocumentoAdicional(BaseModel):
    """Additional document form found in the Doc column (e.g. certificate)."""
    url: str  # form action URL
    token: str  # JWT token value
    param: str  # form param name (e.g. dtaCert)


class Movement(BaseModel):
    folio: int | None
    cuaderno: str
    etapa: str
    tramite: str
    descripcion: str
    fecha: str | None
    foja: int | None
    documento_url: str | None
    documento_token: str | None = None  # JWT for document download
    documento_param: str | None = None  # form param name (dtaDoc or valorDoc)
    # Additional documents in the Doc column (certificates, etc.)
    documentos_adicionales: list[DocumentoAdicional] = []
    # Anexo JWT token extracted from the Anexo column's modal link
    anexo_token: str | None = None
    anexo_func: str | None = None  # JS function name, e.g. "anexoEscritoApelaciones"
    # Appellate-court-specific fields (Suprema / Apelaciones)
    sala: str = ""
    estado: str = ""


class Litigante(BaseModel):
    rol: str
    rut: str
    nombre: str
    persona: str = ""  # "Natural" or "Jurídica"


class DetailResponse(BaseModel):
    metadata: CaseMetadata | dict
    movements: list[Movement] | list[dict]
    litigantes: list[Litigante] | list[dict]
    libro: str | None = None  # top-level convenience field
    blocked: bool
    error: str | None
    # Case-level document tokens
    ebook_token: str = ""  # JWT token for ebook download
    certificado_disponible: bool = False  # whether the Certificado de Envío is downloadable
    # Suprema-specific top-level document tokens
    suprema_docs: list[dict] = []  # list of {tipo, token, func} for textoSuprema/tomoSuprema/documentosSuprema
    # Exhortos and Incompetencia tables
    exhortos: list[dict] = []
    incompetencia: list[dict] = []


class HealthResponse(BaseModel):
    status: str
    last_successful_request: str | None
    pjud_available: bool = True
    uptime_seconds: int
    total_requests: int = 0
    search_requests: int = 0
    detail_requests: int = 0
    familia_requests: int = 0
    total_errors: int = 0
    total_blocked: int = 0
    blocked_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    # Fallos al conseguir sesión del pool. Es la única métrica que se mueve
    # cuando el servicio no llega ni a hablar con OJV: todos los demás contadores
    # se incrementan más adelante en el request, así que en ese escenario quedan
    # en cero y el health se ve sano.
    total_pool_failures: int = 0
    # El complemento del anterior: `total_pool_failures` cuenta cuando NO quedaba
    # ningún bundle sano; esto cuenta cuando había uno más y el reintento por
    # otra IP residencial salvó la consulta. Sin los dos, un pool con 2 de 3
    # bundles quemados se ve igual que uno sano, porque la app recibió su 200.
    total_bundle_retries: int = 0
