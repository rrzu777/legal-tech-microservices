import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from app.cookie_store import DEFAULT_COOKIE_STORE_PATH, validate_cookie_store_path
from app.proxy import sticky_lifetime_seconds

TZ_SANTIAGO = ZoneInfo("America/Santiago")


async def run_query(query):
    """Run a Supabase query chain in a thread to avoid blocking the event loop."""
    return await asyncio.to_thread(query.execute)


class WorkerConfig(BaseSettings):
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str
    WORKER_ID: str = "worker-1"
    POOL_SIZE: int = 1
    BATCH_SIZE: int = 10
    HEARTBEAT_INTERVAL_S: int = 60
    SESSION_MAX_AGE_S: int = 1500
    WORKER_SESSION_REUSE_VALIDATION_ENABLED: bool = False
    SESSION_REUSE_ROLLOUT_STARTED_AT: datetime | None = None
    SESSION_SOFT_VERIFY_AGE_S: int = Field(default=1200, gt=0)
    SESSION_HARD_MAX_AGE_S: int = Field(default=3000, gt=0)
    SESSION_STICKY_SAFETY_MARGIN_S: int = Field(default=600, ge=0)
    OJV_TIMEOUT_S: int = 25
    RATE_LIMIT_MS: int = 2500
    PJUD_BASE_URL: str = "https://oficinajudicialvirtual.pjud.cl"
    LOG_LEVEL: str = "INFO"
    COOKIE_STORE_PATH: str = DEFAULT_COOKIE_STORE_PATH
    MINT_MAX_RETRIES: int = 3
    # Presupuesto total del minteo del worker, incluyendo navegador e
    # inicializacion OJV. El flujo interactivo de la API conserva su propio
    # limite; este valor evita cortar el worker justo despues de que F5 carga.
    MINT_TRAFFIC_BUDGET_S: float = 35.0
    # Pausa del circuit breaker tras un bloqueo. Con el minter, un bloqueo se
    # recupera por re-mint; esta pausa solo rate-limita el re-minteo (evita
    # mint-storms). Configurable por env para tunear throughput sin redeploy.
    BLOCK_PAUSE_S: int = 30
    PJUD_OFF_HOURS_VALIDATION_ONCE: bool = False

    # R2 document storage
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_ENDPOINT: str = ""
    R2_BUCKET: str = "estrado-documents"
    R2_ENABLED: bool = False

    # Familia credential decryption (calls Vercel internal endpoint)
    VERCEL_APP_URL: str = ""
    INTERNAL_CREDENTIALS_API_KEY: str = ""

    # Ops alerting (Telegram)
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # Residential proxy pool (IPRoyal). None = no proxy (legacy single-IP).
    OJV_PROXY_URL: str | None = None
    OJV_PROXY_STICKY_LIFETIME: str = "1h"
    OJV_PROXY_POOL_SIZE: int = 3
    OJV_PROXY_GB_BUDGET: float = 2.0
    OJV_PROXY_GB_ALERT_PCT: int = 80
    OJV_PROXY_PRICE_PER_GB_USD: float = 6.25

    _cookie_store_outside_git = field_validator("COOKIE_STORE_PATH")(
        validate_cookie_store_path
    )

    @field_validator("SESSION_REUSE_ROLLOUT_STARTED_AT", mode="before")
    @classmethod
    def _blank_rollout_cutoff_is_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("OJV_PROXY_STICKY_LIFETIME")
    @classmethod
    def _valid_sticky_lifetime(cls, value: str) -> str:
        sticky_lifetime_seconds(value)
        return value

    @property
    def session_hard_effective_age_s(self) -> int:
        sticky_ceiling = (
            sticky_lifetime_seconds(self.OJV_PROXY_STICKY_LIFETIME)
            - self.SESSION_STICKY_SAFETY_MARGIN_S
        )
        return min(self.SESSION_HARD_MAX_AGE_S, sticky_ceiling)

    @model_validator(mode="after")
    def _valid_session_reuse_ages(self):
        if self.SESSION_SOFT_VERIFY_AGE_S >= self.session_hard_effective_age_s:
            raise ValueError(
                "SESSION_SOFT_VERIFY_AGE_S must be lower than the effective "
                "hard session age"
            )
        rollout_started_at = self.SESSION_REUSE_ROLLOUT_STARTED_AT
        if self.WORKER_SESSION_REUSE_VALIDATION_ENABLED and rollout_started_at is None:
            raise ValueError(
                "SESSION_REUSE_ROLLOUT_STARTED_AT is required when session reuse "
                "validation is enabled"
            )
        if rollout_started_at is not None and rollout_started_at.tzinfo is None:
            raise ValueError("SESSION_REUSE_ROLLOUT_STARTED_AT must be timezone-aware")
        return self

    @field_validator("MINT_TRAFFIC_BUDGET_S")
    @classmethod
    def _valid_mint_traffic_budget(cls, value: float) -> float:
        if not 10.0 <= value <= 60.0:
            raise ValueError("MINT_TRAFFIC_BUDGET_S must be between 10 and 60 seconds")
        return value

    model_config = {"env_file": (".env.worker", ".env"), "env_file_encoding": "utf-8", "extra": "ignore"}
