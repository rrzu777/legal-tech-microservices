from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    API_KEY: str
    OJV_BASE_URL: str = "https://oficinajudicialvirtual.pjud.cl"
    RATE_LIMIT_MS: int = 2500
    LOG_LEVEL: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def get_settings() -> Settings:
    return Settings()
