from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
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
    oidc_issuer: str = "https://auth.example.invalid/"
    oidc_audience: str = "https://api.example.invalid"
    oidc_allowed_algorithms: str = "RS256"
    oidc_jwks_cache_ttl_seconds: int = Field(default=300, gt=0, le=3600)
    oidc_jwks_forced_refresh_cooldown_seconds: int = Field(default=10, ge=1, le=300)
    oidc_http_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    oidc_clock_skew_seconds: int = Field(default=30, ge=0, le=300)

    @field_validator("oidc_issuer")
    @classmethod
    def issuer_must_be_absolute_https_url(cls, value: str) -> str:
        if not value.startswith("https://") or not value.endswith("/"):
            raise ValueError("OIDC issuer must be an absolute HTTPS URL ending in '/'")
        return value

    @property
    def oidc_algorithms(self) -> tuple[str, ...]:
        algorithms = tuple(value.strip() for value in self.oidc_allowed_algorithms.split(","))
        allowed = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
        if not algorithms or any(value not in allowed for value in algorithms):
            raise ValueError("OIDC algorithms must be an explicit asymmetric allowlist")
        return algorithms


@lru_cache
def get_settings() -> Settings:
    return Settings()
