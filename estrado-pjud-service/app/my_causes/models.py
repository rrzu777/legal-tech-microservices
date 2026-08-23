"""Closed data contract emitted by the ``Mis Causas`` listing parser."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


Matter = Literal[
    "suprema",
    "apelaciones",
    "civil",
    "laboral",
    "penal",
    "cobranza",
    "familia",
]
CaseType = Literal["rol", "rit", "ruc"]


class ImportCandidate(BaseModel):
    """Only the fields permitted to cross the discovery persistence boundary."""

    model_config = ConfigDict(extra="forbid")

    matter: Matter
    case_type: CaseType
    case_number: str
    court_code: int | None = None
    court_label: str | None = None
    tribunal_code: int | None = None
    tribunal_label: str | None = None
    libro: str | None = None
    filed_at: date | None = None
    upstream_status: str | None = None
    caption: str | None = None

    @field_validator(
        "case_number",
        "court_label",
        "tribunal_label",
        "libro",
        "upstream_status",
        "caption",
    )
    @classmethod
    def _bound_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        if not cleaned:
            return None
        if len(cleaned) > 500:
            raise ValueError("candidate_text_too_long")
        return cleaned

    @field_validator("case_number")
    @classmethod
    def _require_case_number(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("case_number_required")
        return value

    @field_validator("court_code", "tribunal_code")
    @classmethod
    def _positive_code(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("invalid_upstream_code")
        return value
