from pydantic import BaseModel


class SearchRequest(BaseModel):
    case_type: str  # "rol" | "rit" | "ruc"
    case_number: str  # "X-NNNN-YYYY"
    competencia: str  # "civil" | "laboral" | "cobranza" | "suprema" | "apelaciones" | "penal"
    corte: int = 0  # codigo de corte para apelaciones (0 = todas las cortes)


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


class CaseMetadata(BaseModel):
    rol: str = ""
    tribunal: str = ""
    estado_administrativo: str = ""
    procedimiento: str = ""
    estado_procesal: str = ""
    etapa: str = ""


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
