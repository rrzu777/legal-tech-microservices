from typing import Literal

from pydantic import BaseModel, model_validator

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


class DetailRequest(BaseModel):
    detail_key: str
    competencia: COMPETENCIA_TYPE | None = None


class CaseMetadata(BaseModel):
    rol: str = ""
    tribunal: str = ""
    estado_administrativo: str = ""
    procedimiento: str = ""
    estado_procesal: str = ""
    etapa: str = ""
    # Competencia-specific fields
    ruc: str = ""           # penal
    ubicacion: str = ""     # suprema, apelaciones
    fecha: str = ""         # suprema, apelaciones
    caratulado: str = ""    # suprema
    tipo: str = ""          # suprema
    recurso: str = ""       # apelaciones


class Movement(BaseModel):
    folio: int | None
    cuaderno: str
    etapa: str
    tramite: str
    descripcion: str
    fecha: str | None
    foja: int | None
    documento_url: str | None


class Litigante(BaseModel):
    rol: str
    rut: str
    nombre: str


class DetailResponse(BaseModel):
    metadata: CaseMetadata | dict
    movements: list[Movement] | list[dict]
    litigantes: list[Litigante] | list[dict]
    blocked: bool
    error: str | None


class HealthResponse(BaseModel):
    status: str
    last_successful_request: str | None
    uptime_seconds: int
