import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
from app.rate_limit import limiter
from app.routes import health, search, detail
from app.session_pool import APISessionPool


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    pool = APISessionPool(settings)
    app.state.session_pool = pool

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
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = FastAPI(
        title="estrado-pjud-service",
        version="0.1.0",
        docs_url="/docs",
        lifespan=lifespan,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    app.include_router(health.router)
    app.include_router(search.router)
    app.include_router(detail.router)

    return app


app = create_app()
