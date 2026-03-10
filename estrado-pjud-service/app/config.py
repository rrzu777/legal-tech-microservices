from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    API_KEY: str
    OJV_BASE_URL: str = "https://oficinajudicialvirtual.pjud.cl"
    RATE_LIMIT_MS: int = 2500
    LOG_LEVEL: str = "INFO"
    SESSION_POOL_SIZE: int = 2
    SESSION_MAX_AGE_S: int = 1200

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
