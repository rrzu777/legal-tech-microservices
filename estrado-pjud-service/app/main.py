import logging

from fastapi import FastAPI

from app.config import Settings
from app.routes import health, search, detail


def create_app() -> FastAPI:
    settings = Settings()

    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = FastAPI(
        title="estrado-pjud-service",
        version="0.1.0",
        docs_url="/docs",
    )

    app.include_router(health.router)
    app.include_router(search.router)
    app.include_router(detail.router)

    return app


app = create_app()
