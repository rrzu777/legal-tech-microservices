import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
from app.logging_redaction import install_secret_redaction
from app.rate_limit import limiter
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError
from app.proxy_cost_handler import proxy_cost_control_exception_handler
from app.request_id import LOG_FORMAT, RequestIdFilter, RequestIdMiddleware
from app.usage_context import PjudUsageContextMiddleware
from app.catalogs import CatalogService
from app.routes import health, search, detail, familia, catalogs
from app.session_pool import APISessionPool
from supabase import create_client
from worker.proxy_control import ProxyControl
from worker.proxy_usage import ProxyUsageTracker


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    proxy_control_required = bool(settings.OJV_PROXY_URL)
    proxy_supabase = None
    if settings.SUPABASE_URL and settings.SUPABASE_SERVICE_KEY:
        proxy_supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    proxy_control = (
        ProxyControl(proxy_supabase, actor="estrado-pjud-api")
        if proxy_control_required
        else None
    )
    proxy_usage = ProxyUsageTracker(
        proxy_supabase,
        enabled=proxy_control_required,
        component="api",
        price_per_gb_usd=settings.OJV_PROXY_PRICE_PER_GB_USD,
    )
    pool = APISessionPool(
        settings,
        proxy_control=proxy_control,
        proxy_usage=proxy_usage,
    )
    app.state.session_pool = pool
    app.state.proxy_control = proxy_control
    app.state.proxy_usage = proxy_usage
    app.state.proxy_control_required = proxy_control_required
    app.state.catalog_service = CatalogService(pool, proxy_usage=proxy_usage)
    app.state.catalog_refresh_queue = None

    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
        from app.alert_cooldown_store import (
            AlertCooldownStore,
            DEFAULT_ALERT_COOLDOWN_STORE_PATH,
        )
        from app.alerting import TelegramAlerter
        app.state.alerter = TelegramAlerter(
            bot_token=settings.TELEGRAM_BOT_TOKEN,
            chat_id=settings.TELEGRAM_CHAT_ID,
            blocked_rate_threshold=settings.TELEGRAM_BLOCKED_RATE_THRESHOLD,
            cooldown_seconds=settings.TELEGRAM_COOLDOWN_S,
            event_cooldown_store=AlertCooldownStore(
                DEFAULT_ALERT_COOLDOWN_STORE_PATH
            ),
        )
    else:
        app.state.alerter = None

    try:
        yield
    finally:
        try:
            await pool.close_all()
        finally:
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
    install_secret_redaction(
        root_handlers,
        tuple(filter(None, (
            settings.TELEGRAM_BOT_TOKEN,
            settings.SUPABASE_SERVICE_KEY,
            settings.OJV_PROXY_URL,
        ))),
    )

    app = FastAPI(
        title="estrado-pjud-service",
        version="0.1.0",
        docs_url="/docs",
        lifespan=lifespan,
    )
    app.add_exception_handler(
        ProxyUsagePersistenceError, proxy_cost_control_exception_handler,
    )
    app.add_exception_handler(
        ProxyBudgetExceededError, proxy_cost_control_exception_handler,
    )
    app.add_exception_handler(httpx.ProxyError, proxy_cost_control_exception_handler)

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(PjudUsageContextMiddleware)
    app.add_middleware(RequestIdMiddleware)

    app.include_router(health.router)
    app.include_router(search.router)
    app.include_router(detail.router)
    app.include_router(familia.router)
    app.include_router(catalogs.router)

    return app


app = create_app()
