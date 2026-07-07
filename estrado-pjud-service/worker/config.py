import asyncio
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings

from app.cookie_store import DEFAULT_COOKIE_STORE_PATH

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
    OJV_TIMEOUT_S: int = 25
    RATE_LIMIT_MS: int = 2500
    PJUD_BASE_URL: str = "https://oficinajudicialvirtual.pjud.cl"
    LOG_LEVEL: str = "INFO"
    COOKIE_STORE_PATH: str = DEFAULT_COOKIE_STORE_PATH
    MINT_MAX_RETRIES: int = 3
    # Pausa del circuit breaker tras un bloqueo. Con el minter, un bloqueo se
    # recupera por re-mint; esta pausa solo rate-limita el re-minteo (evita
    # mint-storms). Configurable por env para tunear throughput sin redeploy.
    BLOCK_PAUSE_S: int = 30

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

    model_config = {"env_file": (".env.worker", ".env"), "env_file_encoding": "utf-8", "extra": "ignore"}
