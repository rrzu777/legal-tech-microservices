from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, WithJsonSchema, field_validator, model_validator

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


class PrivateCauseResolutionRequest(BaseModel):
    """Closed request boundary for one explicitly selected Familia candidate."""

    model_config = ConfigDict(extra="forbid")

    rut: RedactedRequestString
    password: RedactedRequestString
    auth_type: Literal["clave_pj"] = "clave_pj"
    case_number: str = Field(pattern=r"^[A-Z]+-[0-9]+-[0-9]{4}$", max_length=128)
    tribunal_code: int | None = Field(default=None, gt=0)
    tribunal_label: str = Field(min_length=1, max_length=200)


class SanitizedCaseDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    caption: str = Field(min_length=1, max_length=500)
    subject: str | None = Field(default=None, max_length=500)
    status: str = Field(min_length=1, max_length=100)
    filed_at: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class SanitizedMovement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["status"]
    label: str = Field(min_length=1, max_length=100)
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class PrivateCauseResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matter: Literal["familia"] = "familia"
    case_type: Literal["rit"] = "rit"
    case_number: str
    tribunal_code: int = Field(gt=0)
    tribunal_label: str
    detail: SanitizedCaseDetail
    movements: list[SanitizedMovement]


PrivateResolutionErrorCode = Literal[
    "private_not_found",
    "private_ambiguous",
    "private_identifier_mismatch",
    "private_tribunal_mismatch",
    "private_evidence_incomplete",
    "private_fence_unavailable",
    "credential_invalid",
    "session_expired",
    "waf",
    "timeout",
    "upstream_changed",
]


class PrivateCauseResolutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    resolution: PrivateCauseResolution | None = None
    error_code: PrivateResolutionErrorCode | None = None

    @model_validator(mode="after")
    def _consistent_result(self):
        if self.ok and (self.resolution is None or self.error_code is not None):
            raise ValueError("successful_resolution_cannot_have_error")
        if not self.ok and (self.resolution is not None or self.error_code is None):
            raise ValueError("failed_resolution_requires_error")
        return self
