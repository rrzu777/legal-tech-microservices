from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings

from app.cookie_store import DEFAULT_COOKIE_STORE_PATH, validate_cookie_store_path
from app.proxy import sticky_lifetime_seconds
from app.runtime_fence import validate_runtime_generation


class Settings(BaseSettings):
    API_KEY: str
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_KEY: str = ""
    PJUD_RUNTIME_GENERATION: str | None = None
    OJV_BASE_URL: str = "https://oficinajudicialvirtual.pjud.cl"
    RATE_LIMIT_MS: int = 2500
    LOG_LEVEL: str = "INFO"
    SESSION_POOL_SIZE: int = 2
    SESSION_MAX_AGE_S: int = 1200
    PRIVATE_RESOLUTION_CONCURRENCY: int = 1
    COOKIE_STORE_PATH: str = DEFAULT_COOKIE_STORE_PATH
    ENABLE_PJUD_PRIVATE_FAMILIA: str = "false"

    @property
    def private_familia_enabled(self) -> bool:
        return self.ENABLE_PJUD_PRIVATE_FAMILIA == "true"

    # Telegram alerts
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_BLOCKED_RATE_THRESHOLD: float = 0.3
    TELEGRAM_COOLDOWN_S: int = 300

    # Residential proxy pool (IPRoyal). None = no proxy (legacy single-IP).
    OJV_PROXY_URL: str | None = None
    OJV_PROXY_STICKY_LIFETIME: str = "1h"
    OJV_PROXY_PRICE_PER_GB_USD: float = 6.25

    _cookie_store_outside_git = field_validator("COOKIE_STORE_PATH")(
        validate_cookie_store_path
    )
    _runtime_generation = field_validator("PJUD_RUNTIME_GENERATION", mode="before")(
        validate_runtime_generation
    )

    @field_validator("OJV_PROXY_STICKY_LIFETIME")
    @classmethod
    def _valid_sticky_lifetime(cls, value: str) -> str:
        sticky_lifetime_seconds(value)
        return value

    @field_validator("PRIVATE_RESOLUTION_CONCURRENCY")
    @classmethod
    def _valid_private_resolution_concurrency(cls, value: int) -> int:
        if isinstance(value, bool) or value < 1:
            raise ValueError("private_resolution_concurrency_must_be_positive")
        return value

    # extra=ignore: el .env es compartido y trae claves del worker (POOL_SIZE,
    # WORKER_ID, OJV_PROXY_POOL_SIZE, etc.) que Settings no define; sin esto
    # pydantic falla al cargar. (Reconcilia un fix que estaba local en el VPS.)
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
