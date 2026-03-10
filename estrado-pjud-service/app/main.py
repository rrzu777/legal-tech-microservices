import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.routes import health, search, detail
from app.session_pool import APISessionPool


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    pool = APISessionPool(settings)
    app.state.session_pool = pool
    try:
        yield
    finally:
        await pool.close_all()


def create_app() -> FastAPI:
    settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = FastAPI(
        title="estrado-pjud-service",
        version="0.1.0",
        docs_url="/docs",
        lifespan=lifespan,
    )

    app.include_router(health.router)
    app.include_router(search.router)
    app.include_router(detail.router)

    return app


app = create_app()
