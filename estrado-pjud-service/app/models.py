from pydantic import BaseModel, model_validator


VALID_CORTE_CODES = {
    0,   # Todas (all courts)
    10,  # C.A. de Arica
    11,  # C.A. de Iquique
    12,  # C.A. de Antofagasta
    13,  # C.A. de Copiapó
    14,  # C.A. de La Serena
    15,  # C.A. de Valparaíso
    16,  # C.A. de Santiago
    17,  # C.A. de San Miguel
    18,  # C.A. de Rancagua
    19,  # C.A. de Talca
    20,  # C.A. de Chillán
    21,  # C.A. de Concepción
    22,  # C.A. de Temuco
    23,  # C.A. de Valdivia
    24,  # C.A. de Puerto Montt
    25,  # C.A. de Coyhaique
    26,  # C.A. de Punta Arenas
}


class SearchRequest(BaseModel):
    case_type: str  # "rol" | "rit" | "ruc"
    case_number: str  # "X-NNNN-YYYY"
    competencia: str  # "suprema" | "apelaciones" | "civil" | "laboral" | "penal" | "cobranza"
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
