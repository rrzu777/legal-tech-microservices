import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
from app.logging_redaction import install_secret_redaction
from app.rate_limit import limiter
from app.request_id import LOG_FORMAT, RequestIdFilter, RequestIdMiddleware
from app.catalogs import CatalogService
from app.routes import health, search, detail, familia, catalogs
from app.session_pool import APISessionPool


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    pool = APISessionPool(settings)
    app.state.session_pool = pool
    app.state.catalog_service = CatalogService(pool)

    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
        from app.alerting import TelegramAlerter
        app.state.alerter = TelegramAlerter(
            bot_token=settings.TELEGRAM_BOT_TOKEN,
            chat_id=settings.TELEGRAM_CHAT_ID,
            blocked_rate_threshold=settings.TELEGRAM_BLOCKED_RATE_THRESHOLD,
            cooldown_seconds=settings.TELEGRAM_COOLDOWN_S,
        )
    else:
        app.state.alerter = None

    try:
        yield
    finally:
        await pool.close_all()
        if hasattr(app.state, 'alerter') and app.state.alerter:
            await app.state.alerter.close()


def create_app() -> FastAPI:
    settings = get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format=LOG_FORMAT,
    )
    # El filter va en el HANDLER raíz, no en un logger puntual: por ahí pasan
    # los records de app.* y de toda lib que propague — cada línea emitida
    # dentro de un request lleva el X-Request-ID que mandó la app (o el acuñado
    # acá). uvicorn.access queda afuera (handler propio, propagate=False).
    # El isinstance-check: los tests llaman create_app() varias veces y sin él
    # se apilan filters duplicados en el mismo handler.
    root_handlers = logging.getLogger().handlers
    for handler in root_handlers:
        if not any(isinstance(f, RequestIdFilter) for f in handler.filters):
            handler.addFilter(RequestIdFilter())
    # httpx registra la URL mediante args diferidos y Telegram lleva el token
    # dentro del path. Redactamos la línea final en el handler raíz: conserva
    # método/host/endpoint y rid, pero el valor configurado nunca llega al
    # journal. El helper es idempotente porque create_app se llama más de una
    # vez en tests y en algunos servidores.
    install_secret_redaction(root_handlers, (settings.TELEGRAM_BOT_TOKEN,))

    app = FastAPI(
        title="estrado-pjud-service",
        version="0.1.0",
        docs_url="/docs",
        lifespan=lifespan,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(RequestIdMiddleware)

    app.include_router(health.router)
    app.include_router(search.router)
    app.include_router(detail.router)
    app.include_router(familia.router)
    app.include_router(catalogs.router)

    return app


app = create_app()
