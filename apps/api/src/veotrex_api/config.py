from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="VEOTREX_", extra="ignore", case_sensitive=False
    )

    environment: str = Field(min_length=1)
    log_level: str = "INFO"
    database_url: SecretStr
    app_version: str = Field(min_length=1)
    service_name: str = "veotrex-api"


@lru_cache
def get_settings() -> Settings:
    return Settings()
