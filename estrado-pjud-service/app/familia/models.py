from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, WithJsonSchema, field_validator

#: ⚠️ CONTRATO CROSS-REPO. Quien lo consume es `classifyFamiliaFailure`
#: (`apps/web/src/lib/pjud/sync-error-patch.ts`, repo LegalTech), que traduce
#: cada codigo a que hacer con la causa: `blocked` es OJV, `session_error` somos
#: nosotros, `invalid_credentials` es terminal. Agregar un codigo aca sin
#: agregarlo alla no rompe nada visible — la app lo manda al default y la causa
#: sigue su curso—, asi que el test de `tests/test_familia_models.py` fija el
#: conjunto para que el cambio no pase inadvertido.
FamiliaErrorCode = Literal["invalid_credentials", "session_error", "no_cases", "parse_error", "blocked"]

_MAX_CASES = 10
RedactedRequestString = Annotated[
    SecretStr,
    WithJsonSchema({"type": "string"}),
]


class FamiliaCaseFilter(BaseModel):
    rit: str   # numeric part, e.g. "123"
    year: str  # e.g. "2024"


class FamiliaSyncRequest(BaseModel):
    rut: RedactedRequestString        # "12345678-9" or "12345678"
    password: RedactedRequestString
    auth_type: Literal["clave_pj", "clave_unica"] = "clave_pj"
    cases: Annotated[list[FamiliaCaseFilter], Field(max_length=_MAX_CASES)] = []

    @field_validator("rut")
    @classmethod
    def _clean_rut(cls, v: SecretStr) -> SecretStr:
        return SecretStr(v.get_secret_value().strip())


class FamiliaCaso(BaseModel):
    rit: str
    tribunal: str
    caratulado: str
    materia: str
    estado: str
    fecha_ingreso: str | None = None


class FamiliaSyncResponse(BaseModel):
    ok: bool
    casos: list[FamiliaCaso]
    error_code: FamiliaErrorCode | None = None
    error: str | None = None
