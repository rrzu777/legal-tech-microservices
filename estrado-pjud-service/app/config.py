from functools import lru_cache

from pydantic_settings import BaseSettings

from app.cookie_store import DEFAULT_COOKIE_STORE_PATH


class Settings(BaseSettings):
    API_KEY: str
    OJV_BASE_URL: str = "https://oficinajudicialvirtual.pjud.cl"
    RATE_LIMIT_MS: int = 2500
    LOG_LEVEL: str = "INFO"
    SESSION_POOL_SIZE: int = 2
    SESSION_MAX_AGE_S: int = 1200
    COOKIE_STORE_PATH: str = DEFAULT_COOKIE_STORE_PATH

    # Telegram alerts
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_BLOCKED_RATE_THRESHOLD: float = 0.3
    TELEGRAM_COOLDOWN_S: int = 300

    # Residential proxy pool (IPRoyal). None = no proxy (legacy single-IP).
    OJV_PROXY_URL: str | None = None
    OJV_PROXY_STICKY_LIFETIME: str = "1h"

    # extra=ignore: el .env es compartido y trae claves del worker (POOL_SIZE,
    # WORKER_ID, OJV_PROXY_POOL_SIZE, etc.) que Settings no define; sin esto
    # pydantic falla al cargar. (Reconcilia un fix que estaba local en el VPS.)
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
