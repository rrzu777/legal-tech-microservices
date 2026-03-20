from typing import Literal

from pydantic import BaseModel, Field, model_validator

COMPETENCIA_TYPE = Literal["suprema", "apelaciones", "civil", "laboral", "penal", "cobranza"]


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


class SearchRequest(BaseModel):
    case_type: str  # "rol" | "rit" | "ruc"
    case_number: str  # "X-NNNN-YYYY"
    competencia: COMPETENCIA_TYPE
    corte: int | None = None  # only valid when competencia == "apelaciones"
    libro: str | None = None

    @model_validator(mode="after")
    def _validate_corte(self):
        if self.corte is not None and self.competencia != "apelaciones":
            raise ValueError("corte is only valid when competencia is 'apelaciones'")
        if self.competencia == "apelaciones" and self.corte is None:
            self.corte = 0
        if self.corte is not None and self.corte not in VALID_CORTE_CODES:
            raise ValueError(
                f"Invalid corte code {self.corte}; must be one of {sorted(VALID_CORTE_CODES)}"
            )
        return self


class CandidateMatch(BaseModel):
    key: str
    rol: str
    tribunal: str
    caratulado: str
    fecha_ingreso: str | None


class SearchResponse(BaseModel):
    found: bool
    match_count: int
    matches: list[CandidateMatch]
    blocked: bool
    error: str | None
    libro_used: str | None = None


class DetailRequest(BaseModel):
    detail_key: str
    competencia: COMPETENCIA_TYPE | None = None
    # Optional search params: when provided, the detail endpoint performs a search
    # on the SAME session before fetching the detail, ensuring JWT + CSRF affinity.
    # This prevents cross-case contamination when session pooling reuses sessions.
    case_number: str | None = None
    corte: int | None = None
    libro: str | None = None


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
    uptime_seconds: int
    total_requests: int = 0
    search_requests: int = 0
    detail_requests: int = 0
    total_errors: int = 0
    total_blocked: int = 0
    blocked_rate: float = Field(default=0.0, ge=0.0, le=1.0)
