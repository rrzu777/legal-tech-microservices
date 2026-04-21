from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator


class FamiliaCaseFilter(BaseModel):
    """Optional filter to scope the search to a specific RIT."""
    rit: str   # numeric part, e.g. "123"
    year: str  # e.g. "2024"


class FamiliaSyncRequest(BaseModel):
    rut: str              # "12345678-9" or "12345678"
    password: str
    auth_type: Literal["clave_pj", "clave_unica"] = "clave_unica"
    # Optional: filter to specific cases. Empty list = return all cases.
    cases: list[FamiliaCaseFilter] = []

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
    # None when ok=True; one of the codes below when ok=False
    error_code: str | None = None
    error: str | None = None

    # error_code values:
    #   "invalid_credentials"  — login rejected by OJV/CU
    #   "session_error"        — network/service error establishing session
    #   "no_cases"             — login OK, zero Familia cases found for this RUT
    #   "parse_error"          — unexpected HTML structure
