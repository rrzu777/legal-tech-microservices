"""Authenticated official PJUD catalog endpoints."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request

from app.auth import verify_api_key
from app.catalogs import CatalogResponse

CatalogCompetencia = Literal["apelaciones", "civil", "laboral", "penal", "cobranza"]

router = APIRouter(prefix="/api/v1/catalogs", tags=["catalogs"])


@router.get("/courts", response_model=CatalogResponse)
async def courts(
    request: Request,
    tipo_busqueda: Annotated[int, Query(ge=0)] = 1,
    _api_key: str = verify_api_key,
):
    return await request.app.state.catalog_service.courts(tipo_busqueda)


@router.get("/tribunals", response_model=CatalogResponse)
async def tribunals(
    request: Request,
    competencia: CatalogCompetencia,
    corte: Annotated[int, Query(gt=0)],
    tipo_busqueda: Annotated[int, Query(ge=0)] = 1,
    _api_key: str = verify_api_key,
):
    return await request.app.state.catalog_service.tribunals(
        competencia, corte, tipo_busqueda
    )


@router.get("/books", response_model=CatalogResponse)
async def books(
    request: Request,
    competencia: CatalogCompetencia,
    anno: Annotated[int, Query(ge=2022, le=2026)],
    corte: Annotated[int | None, Query(gt=0)] = None,
    _api_key: str = verify_api_key,
):
    return await request.app.state.catalog_service.books(competencia, corte, anno)
