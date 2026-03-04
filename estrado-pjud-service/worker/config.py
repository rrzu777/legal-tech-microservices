from pydantic_settings import BaseSettings


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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
