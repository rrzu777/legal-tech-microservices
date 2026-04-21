from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator

FamiliaErrorCode = Literal["invalid_credentials", "session_error", "no_cases", "parse_error"]


class FamiliaCaseFilter(BaseModel):
    rit: str   # numeric part, e.g. "123"
    year: str  # e.g. "2024"


class FamiliaSyncRequest(BaseModel):
    rut: str              # "12345678-9" or "12345678"
    password: str
    auth_type: Literal["clave_pj", "clave_unica"] = "clave_unica"
    cases: list[FamiliaCaseFilter] = []  # empty = return all cases

    @field_validator("rut")
    @classmethod
    def _clean_rut(cls, v: str) -> str:
        return v.strip()


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
