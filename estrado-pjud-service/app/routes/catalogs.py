"""Authenticated official PJUD catalog endpoints."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request

from app.auth import verify_api_key
from app.catalogs import CatalogResponse

COURT_CODES = {10, 11, 15, 20, 25, 30, 35, 40, 45, 46, 50, 55, 56, 60, 61, 90, 91}

CatalogCompetencia = Literal["apelaciones", "civil", "laboral", "penal", "cobranza"]

router = APIRouter(prefix="/api/v1/catalogs", tags=["catalogs"])


@router.get("/courts", response_model=CatalogResponse)
async def courts(
    request: Request,
    tipo_busqueda: Annotated[int, Query(le=1, ge=1)] = 1,
    _api_key: str = verify_api_key,
):
    return await request.app.state.catalog_service.courts(tipo_busqueda)


@router.get("/tribunals", response_model=CatalogResponse)
async def tribunals(
    request: Request,
    competencia: CatalogCompetencia,
    corte: Annotated[int, Query(ge=10, le=91)],
    tipo_busqueda: Annotated[int, Query(le=1, ge=1)] = 1,
    _api_key: str = verify_api_key,
):
    if corte not in COURT_CODES:
        from fastapi import HTTPException
        raise HTTPException(422, "Invalid official court code")
    return await request.app.state.catalog_service.tribunals(
        competencia, corte, tipo_busqueda
    )


@router.get("/books", response_model=CatalogResponse)
async def books(
    request: Request,
    competencia: CatalogCompetencia,
    anno: Annotated[int, Query(ge=2022, le=2026)],
    corte: Annotated[int | None, Query(ge=10, le=91)] = None,
    _api_key: str = verify_api_key,
):
    if corte is not None and corte not in COURT_CODES:
        from fastapi import HTTPException
        raise HTTPException(422, "Invalid official court code")
    return await request.app.state.catalog_service.books(competencia, corte, anno)
