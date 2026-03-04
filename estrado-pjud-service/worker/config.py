import asyncio
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings

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

    model_config = {"env_file": (".env.worker", ".env"), "env_file_encoding": "utf-8", "extra": "ignore"}
